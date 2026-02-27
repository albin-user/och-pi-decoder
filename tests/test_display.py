"""Tests for the display module — HDMI resolution management."""

import asyncio
import platform
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_decoder.display import (
    get_available_modes,
    get_current_resolution,
    get_pi_model,
    get_refresh_rates_for_resolution,
    set_display_resolution,
    _FALLBACK_MODES,
    _find_drm_status_path,
    _read_drm_status,
    monitor_hdmi_hotplug,
)


class TestGetAvailableModes:
    @patch("platform.system", return_value="Darwin")
    def test_fallback_on_non_linux(self, _mock):
        modes = get_available_modes()
        assert modes == _FALLBACK_MODES

    @patch("platform.system", return_value="Linux")
    def test_fallback_on_missing_sysfs(self, _mock):
        with patch("glob.glob", return_value=[]):
            modes = get_available_modes()
        assert modes == _FALLBACK_MODES

    @patch("platform.system", return_value="Linux")
    def test_reads_modes_from_sysfs(self, _mock, tmp_path):
        modes_file = tmp_path / "modes"
        modes_file.write_text("1920x1080\n1280x720\n720x480\n")

        with patch("glob.glob", return_value=[str(modes_file)]):
            modes = get_available_modes()

        assert "1920x1080" in modes
        assert "1280x720" in modes
        assert "720x480" in modes

    @patch("platform.system", return_value="Linux")
    def test_deduplicates_modes(self, _mock, tmp_path):
        modes_file = tmp_path / "modes"
        modes_file.write_text("1920x1080\n1920x1080\n1280x720\n")

        with patch("glob.glob", return_value=[str(modes_file)]):
            modes = get_available_modes()

        assert modes.count("1920x1080") == 1

    @patch("platform.system", return_value="Linux")
    def test_strips_interlace_suffix(self, _mock, tmp_path):
        modes_file = tmp_path / "modes"
        modes_file.write_text("1920x1080i\n1280x720p\n")

        with patch("glob.glob", return_value=[str(modes_file)]):
            modes = get_available_modes()

        assert "1920x1080" in modes
        assert "1280x720" in modes


class TestGetPiModel:
    def test_detects_pi5(self, tmp_path):
        model_file = tmp_path / "model"
        model_file.write_text("Raspberry Pi 5 Model B Rev 1.0\x00")
        with patch("pi_decoder.display._PI_MODEL_PATH", model_file):
            assert get_pi_model() == 5

    def test_detects_pi4(self, tmp_path):
        model_file = tmp_path / "model"
        model_file.write_text("Raspberry Pi 4 Model B Rev 1.4\x00")
        with patch("pi_decoder.display._PI_MODEL_PATH", model_file):
            assert get_pi_model() == 4

    def test_falls_back_to_4_on_missing(self, tmp_path):
        missing = tmp_path / "nonexistent"
        with patch("pi_decoder.display._PI_MODEL_PATH", missing):
            assert get_pi_model() == 4

    def test_falls_back_to_4_on_unknown(self, tmp_path):
        model_file = tmp_path / "model"
        model_file.write_text("Some Other Device")
        with patch("pi_decoder.display._PI_MODEL_PATH", model_file):
            assert get_pi_model() == 4


class TestGetRefreshRatesForResolution:
    def test_1080p_gets_all_rates(self):
        rates = get_refresh_rates_for_resolution("1920x1080", pi_model=4)
        assert rates == [24, 25, 30, 50, 60]

    def test_4k_pi4_limited(self):
        rates = get_refresh_rates_for_resolution("3840x2160", pi_model=4)
        assert rates == [24, 25, 30]

    def test_4k_pi5_all_rates(self):
        rates = get_refresh_rates_for_resolution("3840x2160", pi_model=5)
        assert rates == [24, 25, 30, 50, 60]

    def test_720p_gets_all_rates(self):
        rates = get_refresh_rates_for_resolution("1280x720", pi_model=4)
        assert rates == [24, 25, 30, 50, 60]

    def test_auto_detects_pi_model(self, tmp_path):
        model_file = tmp_path / "model"
        model_file.write_text("Raspberry Pi 4 Model B Rev 1.4\x00")
        with patch("pi_decoder.display._PI_MODEL_PATH", model_file):
            rates = get_refresh_rates_for_resolution("3840x2160")
        assert rates == [24, 25, 30]


class TestGetCurrentResolution:
    def test_returns_empty_when_no_cmdline(self):
        with patch("pi_decoder.display._find_cmdline_path", return_value=None):
            result = get_current_resolution()
        assert result == ""

    def test_parses_video_param(self, tmp_path):
        cmdline = tmp_path / "cmdline.txt"
        cmdline.write_text("console=tty1 video=HDMI-A-1:1920x1080@60D root=/dev/mmcblk0p2")

        with patch("pi_decoder.display._find_cmdline_path", return_value=cmdline):
            result = get_current_resolution()

        assert result == "1920x1080@60D"

    def test_returns_empty_when_no_video_param(self, tmp_path):
        cmdline = tmp_path / "cmdline.txt"
        cmdline.write_text("console=tty1 root=/dev/mmcblk0p2")

        with patch("pi_decoder.display._find_cmdline_path", return_value=cmdline):
            result = get_current_resolution()

        assert result == ""

    def test_handles_different_resolution(self, tmp_path):
        cmdline = tmp_path / "cmdline.txt"
        cmdline.write_text("console=tty1 video=HDMI-A-1:1280x720@50D root=/dev/mmcblk0p2")

        with patch("pi_decoder.display._find_cmdline_path", return_value=cmdline):
            result = get_current_resolution()

        assert result == "1280x720@50D"


class TestSetDisplayResolution:
    @patch("platform.system", return_value="Darwin")
    async def test_skips_on_non_linux(self, _mock):
        # Should not raise
        await set_display_resolution("1920x1080@60D")

    @patch("platform.system", return_value="Linux")
    async def test_raises_when_no_cmdline(self, _mock):
        with patch("pi_decoder.display._find_cmdline_path", return_value=None):
            with pytest.raises(FileNotFoundError):
                await set_display_resolution("1920x1080@60D")

    @patch("platform.system", return_value="Linux")
    async def test_writes_new_resolution(self, _mock, tmp_path):
        cmdline = tmp_path / "cmdline.txt"
        cmdline.write_text("console=tty1 video=HDMI-A-1:1920x1080@60D root=/dev/mmcblk0p2")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("pi_decoder.display._find_cmdline_path", return_value=cmdline), \
             patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc), \
             patch("asyncio.wait_for", new_callable=AsyncMock, return_value=(b"", b"")):
            await set_display_resolution("1280x720@50D")

        # Verify the content written to stdin contains the new resolution
        call_args = mock_proc.communicate.call_args
        content = call_args[1]["input"].decode() if "input" in call_args[1] else call_args[0][0].decode() if call_args[0] else ""
        # The old video= param should be stripped and new one appended
        assert "video=HDMI-A-1:1280x720@50D" in content
        assert "video=HDMI-A-1:1920x1080@60D" not in content

    @patch("platform.system", return_value="Linux")
    async def test_adds_resolution_when_none_existed(self, _mock, tmp_path):
        cmdline = tmp_path / "cmdline.txt"
        cmdline.write_text("console=tty1 root=/dev/mmcblk0p2")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("pi_decoder.display._find_cmdline_path", return_value=cmdline), \
             patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc), \
             patch("asyncio.wait_for", new_callable=AsyncMock, return_value=(b"", b"")):
            await set_display_resolution("1920x1080@60D")

        call_args = mock_proc.communicate.call_args
        content = call_args[1]["input"].decode() if "input" in call_args[1] else call_args[0][0].decode() if call_args[0] else ""
        assert "video=HDMI-A-1:1920x1080@60D" in content
        assert "console=tty1" in content


# ── HDMI hotplug monitoring ──────────────────────────────────────────────


class TestFindDrmStatusPath:
    @patch("platform.system", return_value="Darwin")
    def test_returns_none_on_non_linux(self, _mock):
        assert _find_drm_status_path() is None

    @patch("platform.system", return_value="Linux")
    def test_returns_none_when_no_connectors(self, _mock):
        with patch("glob.glob", return_value=[]):
            assert _find_drm_status_path() is None

    @patch("platform.system", return_value="Linux")
    def test_returns_first_connector(self, _mock):
        paths = [
            "/sys/class/drm/card1-HDMI-A-1/status",
            "/sys/class/drm/card0-HDMI-A-1/status",
        ]
        with patch("glob.glob", return_value=sorted(paths)):
            result = _find_drm_status_path()
        assert result == "/sys/class/drm/card0-HDMI-A-1/status"


class TestReadDrmStatus:
    def test_reads_connected(self, tmp_path):
        status_file = tmp_path / "status"
        status_file.write_text("connected\n")
        assert _read_drm_status(str(status_file)) == "connected"

    def test_reads_disconnected(self, tmp_path):
        status_file = tmp_path / "status"
        status_file.write_text("disconnected\n")
        assert _read_drm_status(str(status_file)) == "disconnected"

    def test_returns_unknown_on_missing_file(self):
        assert _read_drm_status("/nonexistent/path") == "unknown"


class TestMonitorHdmiHotplug:
    @patch("pi_decoder.display._find_drm_status_path", return_value=None)
    async def test_exits_immediately_on_non_linux(self, _mock):
        callback = AsyncMock()
        await monitor_hdmi_hotplug(callback)
        callback.assert_not_awaited()

    @patch("pi_decoder.display._find_drm_status_path", return_value="/sys/class/drm/card1-HDMI-A-1/status")
    async def test_calls_callback_on_hotplug(self, _mock_path):
        callback = AsyncMock()
        poll_count = 0
        statuses = ["disconnected", "connected", "connected"]  # initial, poll, re-check after EDID

        def fake_read(path):
            nonlocal poll_count
            result = statuses[min(poll_count, len(statuses) - 1)]
            poll_count += 1
            return result

        async def fake_sleep(duration):
            # After callback is called, raise to exit the loop
            if callback.await_count > 0:
                raise asyncio.CancelledError

        with patch("pi_decoder.display._read_drm_status", side_effect=fake_read), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await monitor_hdmi_hotplug(callback, interval=0.01)

        callback.assert_awaited_once()

    @patch("pi_decoder.display._find_drm_status_path", return_value="/sys/class/drm/card1-HDMI-A-1/status")
    async def test_no_callback_when_stays_connected(self, _mock_path):
        callback = AsyncMock()
        poll_count = 0

        def fake_read(path):
            return "connected"

        async def fake_sleep(duration):
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 3:
                raise asyncio.CancelledError

        with patch("pi_decoder.display._read_drm_status", side_effect=fake_read), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await monitor_hdmi_hotplug(callback, interval=0.01)

        callback.assert_not_awaited()

    @patch("pi_decoder.display._find_drm_status_path", return_value="/sys/class/drm/card1-HDMI-A-1/status")
    async def test_skips_restart_if_disconnected_during_edid_wait(self, _mock_path):
        callback = AsyncMock()
        poll_count = 0
        # initial=disconnected, poll=connected, re-check after EDID=disconnected
        statuses = ["disconnected", "connected", "disconnected"]

        def fake_read(path):
            nonlocal poll_count
            result = statuses[min(poll_count, len(statuses) - 1)]
            poll_count += 1
            return result

        sleep_count = 0

        async def fake_sleep(duration):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 3:
                raise asyncio.CancelledError

        with patch("pi_decoder.display._read_drm_status", side_effect=fake_read), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await monitor_hdmi_hotplug(callback, interval=0.01)

        callback.assert_not_awaited()

    @patch("pi_decoder.display._find_drm_status_path", return_value="/sys/class/drm/card1-HDMI-A-1/status")
    async def test_callback_exception_does_not_stop_monitor(self, _mock_path):
        callback = AsyncMock(side_effect=[RuntimeError("restart failed"), None])
        poll_count = 0
        # Two hotplug events: both disconnected→connected
        statuses = [
            "disconnected",  # initial
            "connected",     # first poll
            "connected",     # first EDID re-check
            "disconnected",  # second poll (after first callback)
            "connected",     # third poll
            "connected",     # second EDID re-check
        ]

        def fake_read(path):
            nonlocal poll_count
            result = statuses[min(poll_count, len(statuses) - 1)]
            poll_count += 1
            return result

        async def fake_sleep(duration):
            if callback.await_count >= 2:
                raise asyncio.CancelledError

        with patch("pi_decoder.display._read_drm_status", side_effect=fake_read), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await monitor_hdmi_hotplug(callback, interval=0.01)

        assert callback.await_count == 2
