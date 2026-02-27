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
