"""Tests for pi_decoder.hostname module."""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from pi_decoder.hostname import sanitize_hostname, set_hostname


# ── sanitize_hostname tests ──────────────────────────────────────────


class TestSanitizeHostname:
    def test_lowercase(self):
        assert sanitize_hostname("Sanctuary") == "sanctuary"

    def test_spaces_to_hyphens(self):
        assert sanitize_hostname("Overflow Room") == "overflow-room"

    def test_underscores_to_hyphens(self):
        assert sanitize_hostname("My_Decoder") == "my-decoder"

    def test_strips_special_chars(self):
        assert sanitize_hostname("My_Decoder!!") == "my-decoder"

    def test_empty_fallback(self):
        assert sanitize_hostname("") == "pi-decoder"

    def test_only_hyphens_fallback(self):
        assert sanitize_hostname("---") == "pi-decoder"

    def test_only_special_chars_fallback(self):
        assert sanitize_hostname("!!!") == "pi-decoder"

    def test_truncates_to_63(self):
        long_name = "a" * 70
        result = sanitize_hostname(long_name)
        assert len(result) == 63
        assert result == "a" * 63

    def test_upper_case_with_spaces(self):
        assert sanitize_hostname("UPPER CASE") == "upper-case"

    def test_collapses_multiple_hyphens(self):
        assert sanitize_hostname("foo - - bar") == "foo-bar"

    def test_strips_leading_trailing_hyphens(self):
        assert sanitize_hostname("-hello-") == "hello"

    def test_mixed_special_chars(self):
        assert sanitize_hostname("Café & Lïbrary!") == "caf-lbrary"

    def test_numbers_preserved(self):
        assert sanitize_hostname("Room 101") == "room-101"

    def test_already_valid(self):
        assert sanitize_hostname("my-decoder") == "my-decoder"


# ── set_hostname tests ───────────────────────────────────────────────


class TestSetHostname:
    @pytest.mark.asyncio
    async def test_returns_sanitized_hostname(self):
        """set_hostname always returns the sanitized hostname."""
        with patch("pi_decoder.hostname.platform") as mock_platform:
            mock_platform.system.return_value = "Darwin"
            result = await set_hostname("Overflow Room")
            assert result == "overflow-room"

    @pytest.mark.asyncio
    async def test_skips_on_non_linux(self):
        """On non-Linux, logs debug and returns without running commands."""
        with patch("pi_decoder.hostname.platform") as mock_platform, \
             patch("pi_decoder.hostname.asyncio.create_subprocess_exec") as mock_exec:
            mock_platform.system.return_value = "Darwin"
            result = await set_hostname("Test")
            assert result == "test"
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_hostnamectl(self):
        """On Linux, calls hostnamectl with sanitized name."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("pi_decoder.hostname.platform") as mock_platform, \
             patch("pi_decoder.hostname.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            mock_platform.system.return_value = "Linux"
            result = await set_hostname("Sanctuary")
            assert result == "sanctuary"
            # First call should be hostnamectl
            first_call = mock_exec.call_args_list[0]
            assert first_call[0] == ("sudo", "hostnamectl", "set-hostname", "sanctuary")

    @pytest.mark.asyncio
    async def test_updates_etc_hosts(self):
        """On Linux, also writes /etc/hosts via tee."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        mock_hosts = "127.0.0.1\tlocalhost\n127.0.1.1\told-hostname\n"
        with patch("pi_decoder.hostname.platform") as mock_platform, \
             patch("pi_decoder.hostname.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec, \
             patch("pi_decoder.hostname.Path") as mock_path_cls:
            mock_platform.system.return_value = "Linux"
            mock_path_cls.return_value.read_text.return_value = mock_hosts
            await set_hostname("Sanctuary")
            # Should have two calls: hostnamectl and tee
            assert mock_exec.call_count == 2
            tee_call = mock_exec.call_args_list[1]
            assert tee_call[0][0:2] == ("sudo", "tee")
            assert "/etc/hosts" in tee_call[0]

    @pytest.mark.asyncio
    async def test_failure_logs_warning_no_exception(self):
        """If hostnamectl fails, logs warning but doesn't raise."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"permission denied"))
        mock_proc.returncode = 1

        with patch("pi_decoder.hostname.platform") as mock_platform, \
             patch("pi_decoder.hostname.asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("pi_decoder.hostname.log") as mock_log:
            mock_platform.system.return_value = "Linux"
            result = await set_hostname("Test")
            assert result == "test"
            mock_log.warning.assert_called()

    @pytest.mark.asyncio
    async def test_timeout_logs_warning_no_exception(self):
        """If hostnamectl times out, logs warning but doesn't raise."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("pi_decoder.hostname.platform") as mock_platform, \
             patch("pi_decoder.hostname.asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("pi_decoder.hostname.log") as mock_log:
            mock_platform.system.return_value = "Linux"
            result = await set_hostname("Test")
            assert result == "test"
            mock_log.warning.assert_called()

    @pytest.mark.asyncio
    async def test_exception_logs_warning_no_raise(self):
        """If subprocess creation fails entirely, logs warning but doesn't raise."""
        with patch("pi_decoder.hostname.platform") as mock_platform, \
             patch("pi_decoder.hostname.asyncio.create_subprocess_exec", side_effect=FileNotFoundError("hostnamectl")), \
             patch("pi_decoder.hostname.log") as mock_log:
            mock_platform.system.return_value = "Linux"
            result = await set_hostname("Test")
            assert result == "test"
            mock_log.warning.assert_called()
