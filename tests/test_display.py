"""Tests for the display module — HDMI resolution management."""

import asyncio
import platform
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_decoder.display import (
    get_available_modes,
    get_current_resolution,
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
