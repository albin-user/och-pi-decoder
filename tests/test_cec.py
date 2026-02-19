"""Tests for CEC TV control module."""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from pi_decoder import cec


def make_proc(stdout: str = "", returncode: int = 0):
    """Create a mock subprocess result."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestRunCec:
    """Test the low-level _run_cec helper."""

    @pytest.mark.asyncio
    async def test_sends_command_to_stdin(self):
        proc = make_proc("done")
        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            result = await cec._run_cec("on 0")
        assert result == "done"
        proc.communicate.assert_called_once_with(input=b"on 0")

    @pytest.mark.asyncio
    async def test_passes_cec_client_args(self):
        proc = make_proc("ok")
        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await cec._run_cec("standby 0")
        # Verify cec-client is called with -s -d 1
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "cec-client"
        assert "-s" in args
        assert "-d" in args
        assert "1" in args

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(TimeoutError, match="timed out"):
                await cec._run_cec("on 0")
        proc.kill.assert_called_once()


class TestPowerCommands:

    def setup_method(self):
        """Reset CEC power cache between tests."""
        cec._power_cache = "unknown"
        cec._power_cache_time = 0.0
        cec._power_lock = None

    @pytest.mark.asyncio
    async def test_power_on(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock, return_value="power on sent"):
            result = await cec.power_on()
        assert "power on sent" in result

    @pytest.mark.asyncio
    async def test_standby(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock, return_value="standby sent"):
            result = await cec.standby()
        assert "standby sent" in result

    @pytest.mark.asyncio
    async def test_get_power_status_on(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock,
                    return_value="power status: on\n"):
            result = await cec.get_power_status()
        assert result == "on"

    @pytest.mark.asyncio
    async def test_get_power_status_standby(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock,
                    return_value="power status: standby\n"):
            result = await cec.get_power_status()
        assert result == "standby"

    @pytest.mark.asyncio
    async def test_get_power_status_unknown(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock,
                    return_value="some garbage output\n"):
            result = await cec.get_power_status()
        assert result == "unknown"

    @pytest.mark.asyncio
    async def test_get_power_status_uses_cache(self):
        """Second call within TTL returns cached value without spawning cec-client."""
        mock = AsyncMock(return_value="power status: on\n")
        with patch("pi_decoder.cec._run_cec", mock):
            result1 = await cec.get_power_status()
            result2 = await cec.get_power_status()
        assert result1 == result2 == "on"
        mock.assert_called_once()  # Only one subprocess spawned


class TestSourceCommands:

    @pytest.mark.asyncio
    async def test_active_source(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock, return_value="as sent"):
            result = await cec.active_source()
        assert "as sent" in result

    @pytest.mark.asyncio
    async def test_set_input_valid(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock, return_value="tx sent") as mock:
            result = await cec.set_input(2)
        mock.assert_called_once_with("tx 4F:82:20:00")

    @pytest.mark.asyncio
    async def test_set_input_invalid_port(self):
        with pytest.raises(ValueError, match="1-4"):
            await cec.set_input(5)

    @pytest.mark.asyncio
    async def test_set_input_port_zero(self):
        with pytest.raises(ValueError, match="1-4"):
            await cec.set_input(0)


class TestVolumeCommands:

    @pytest.mark.asyncio
    async def test_volume_up(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock, return_value="vol up"):
            result = await cec.volume_up()
        assert result == "vol up"

    @pytest.mark.asyncio
    async def test_volume_down(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock, return_value="vol down"):
            result = await cec.volume_down()
        assert result == "vol down"

    @pytest.mark.asyncio
    async def test_mute(self):
        with patch("pi_decoder.cec._run_cec", new_callable=AsyncMock, return_value="muted"):
            result = await cec.mute()
        assert result == "muted"
