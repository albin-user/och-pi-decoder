"""Tests for CEC TV control module."""

import asyncio
import time
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
        # Verify cec-client is called with -s -d 1 and -o Pi-Decoder (OSD name)
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "cec-client"
        assert "-s" in args
        assert "-d" in args
        assert "1" in args
        assert "-o" in args
        assert "Pi-Decoder" in args

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

    @pytest.mark.asyncio
    async def test_concurrent_calls_are_serialised(self):
        """Multiple concurrent _run_cec calls must NOT spawn overlapping subprocesses.

        Without the lock, simultaneous calls race on /dev/cec0 and fail with EBUSY
        (errno 16). The lock should queue them so only one subprocess runs at a time.
        """
        cec._cec_lock = None  # reset so the test gets a fresh lock
        active_count = 0
        max_active = 0

        async def slow_communicate(input=None):
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            await asyncio.sleep(0.05)  # simulate cec-client work
            active_count -= 1
            return (b"done", b"")

        def make_proc(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = slow_communicate
            proc.kill = MagicMock()
            proc.wait = AsyncMock()
            return proc

        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", side_effect=lambda *a, **k: make_proc()):
            await asyncio.gather(
                cec._run_cec("on 0"),
                cec._run_cec("on 0"),
                cec._run_cec("on 0"),
                cec._run_cec("on 0"),
            )
        assert max_active == 1, f"Expected serialised calls; saw {max_active} concurrent"


class TestPowerCommands:

    def setup_method(self):
        """Reset CEC power cache between tests."""
        cec._power_cache = "unknown"
        cec._power_cache_time = 0.0
        cec._cec_lock = None

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

    @pytest.mark.asyncio
    async def test_get_power_status_cache_expires(self):
        """After TTL expires, a fresh cec-client query is made."""
        mock = AsyncMock(side_effect=[
            "power status: on\n",
            "power status: standby\n",
        ])
        with patch("pi_decoder.cec._run_cec", mock):
            result1 = await cec.get_power_status()
            assert result1 == "on"
            # Force cache expiry
            cec._power_cache_time = time.monotonic() - cec._POWER_CACHE_TTL - 1
            result2 = await cec.get_power_status()
            assert result2 == "standby"
        assert mock.call_count == 2

    @pytest.mark.asyncio
    async def test_get_power_status_error_returns_unknown(self):
        """If cec-client fails, return 'unknown' instead of crashing."""
        mock = AsyncMock(side_effect=Exception("cec-client crashed"))
        with patch("pi_decoder.cec._run_cec", mock):
            result = await cec.get_power_status()
        assert result == "unknown"

    @pytest.mark.asyncio
    async def test_power_on_invalidates_cache(self):
        """power_on() should invalidate cache so next status query is fresh."""
        mock_run = AsyncMock(return_value="power status: standby\n")
        with patch("pi_decoder.cec._run_cec", mock_run):
            # Prime the cache
            await cec.get_power_status()
            assert cec._power_cache == "standby"
            # power_on should invalidate
            mock_run.return_value = "power on sent"
            await cec.power_on()
            assert cec._power_cache_time == 0.0

    @pytest.mark.asyncio
    async def test_standby_invalidates_cache(self):
        """standby() should invalidate cache so next status query is fresh."""
        mock_run = AsyncMock(return_value="power status: on\n")
        with patch("pi_decoder.cec._run_cec", mock_run):
            await cec.get_power_status()
            assert cec._power_cache == "on"
            mock_run.return_value = "standby sent"
            await cec.standby()
            assert cec._power_cache_time == 0.0


class TestIsAvailable:

    def setup_method(self):
        cec._cec_available = None

    def test_available_when_binary_exists(self):
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-client"):
            assert cec.is_available() is True

    def test_not_available_when_binary_missing(self):
        with patch("pi_decoder.cec.shutil.which", return_value=None):
            assert cec.is_available() is False

    def test_result_is_cached(self):
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-client") as mock:
            cec.is_available()
            cec.is_available()
        mock.assert_called_once()


class TestConfigureAdapter:

    @pytest.mark.asyncio
    async def test_configure_adapter_success(self):
        proc = make_proc("")
        proc.returncode = 0
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            ok = await cec.configure_adapter()
        assert ok is True
        args = mock_exec.call_args[0]
        assert args[0] == "sudo"
        assert "cec-ctl" in args
        assert "--playback" in args
        assert "--osd-name" in args
        assert "Pi-Decoder" in args

    @pytest.mark.asyncio
    async def test_configure_adapter_missing_binary(self):
        with patch("pi_decoder.cec.shutil.which", return_value=None):
            ok = await cec.configure_adapter()
        assert ok is False

    @pytest.mark.asyncio
    async def test_configure_adapter_nonzero_exit(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"permission denied"))
        proc.returncode = 1
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            ok = await cec.configure_adapter()
        assert ok is False

    @pytest.mark.asyncio
    async def test_configure_adapter_timeout(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            ok = await cec.configure_adapter()
        assert ok is False


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
    """Volume / mute run via cec-ctl fast path (_key_tap), not cec-client."""

    def setup_method(self):
        cec._cec_lock = None

    @pytest.mark.asyncio
    async def test_volume_up_default_1_step(self):
        with patch("pi_decoder.cec._key_tap", new_callable=AsyncMock, return_value=True) as mock_tap:
            result = await cec.volume_up()
        assert result == {"ok": True, "sent": 1, "dropped": False}
        mock_tap.assert_called_once_with("volume-up")

    @pytest.mark.asyncio
    async def test_volume_up_multiple_steps(self):
        with patch("pi_decoder.cec._key_tap", new_callable=AsyncMock, return_value=True) as mock_tap:
            result = await cec.volume_up(steps=5)
        assert result == {"ok": True, "sent": 5, "dropped": False}
        assert mock_tap.call_count == 5

    @pytest.mark.asyncio
    async def test_volume_up_clamped_to_max(self):
        with patch("pi_decoder.cec._key_tap", new_callable=AsyncMock, return_value=True) as mock_tap:
            await cec.volume_up(steps=999)
        assert mock_tap.call_count == 20  # safety cap

    @pytest.mark.asyncio
    async def test_volume_up_clamped_to_min(self):
        with patch("pi_decoder.cec._key_tap", new_callable=AsyncMock, return_value=True) as mock_tap:
            result = await cec.volume_up(steps=0)
        assert result["sent"] == 1
        assert mock_tap.call_count == 1

    @pytest.mark.asyncio
    async def test_volume_down(self):
        with patch("pi_decoder.cec._key_tap", new_callable=AsyncMock, return_value=True) as mock_tap:
            result = await cec.volume_down(steps=3)
        assert result == {"ok": True, "sent": 3, "dropped": False}
        for call in mock_tap.call_args_list:
            assert call.args == ("volume-down",)

    @pytest.mark.asyncio
    async def test_mute(self):
        with patch("pi_decoder.cec._key_tap", new_callable=AsyncMock, return_value=True) as mock_tap:
            result = await cec.mute()
        assert result == {"ok": True, "sent": 1, "dropped": False}
        mock_tap.assert_called_once_with("mute")

    @pytest.mark.asyncio
    async def test_drops_when_busy(self):
        """If the adapter lock is held, volume commands drop instead of queueing."""
        with patch("pi_decoder.cec._is_busy", return_value=True), \
             patch("pi_decoder.cec._key_tap", new_callable=AsyncMock) as mock_tap:
            result = await cec.volume_up(steps=3)
        assert result == {"ok": True, "sent": 0, "dropped": True}
        mock_tap.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_on_tap_failure(self):
        """If one tap fails mid-burst, stop and report how many succeeded."""
        side_effects = [True, True, False, True]  # 3rd fails
        with patch("pi_decoder.cec._key_tap", new_callable=AsyncMock, side_effect=side_effects) as mock_tap:
            result = await cec.volume_up(steps=4)
        assert result["sent"] == 2
        assert mock_tap.call_count == 3  # stops after failure


class TestKeyTap:
    """Direct tests of the _key_tap + _run_cec_ctl fast-path helpers."""

    def setup_method(self):
        cec._cec_lock = None

    @pytest.mark.asyncio
    async def test_key_tap_sends_pressed_then_released(self):
        ok_proc = AsyncMock()
        ok_proc.communicate = AsyncMock(return_value=(b"", b""))
        ok_proc.returncode = 0
        calls: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return ok_proc

        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await cec._key_tap("volume-up")
        assert ok is True
        assert len(calls) == 2
        assert "--user-control-pressed" in calls[0]
        assert "ui-cmd=volume-up" in calls[0]
        assert "--user-control-released" in calls[1]

    @pytest.mark.asyncio
    async def test_key_tap_unknown_key_raises(self):
        with pytest.raises(ValueError, match="Unknown key"):
            await cec._key_tap("bogus")

    @pytest.mark.asyncio
    async def test_key_tap_returns_false_if_press_fails(self):
        fail_proc = AsyncMock()
        fail_proc.communicate = AsyncMock(return_value=(b"", b"err"))
        fail_proc.returncode = 1
        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=fail_proc):
            ok = await cec._key_tap("volume-up")
        assert ok is False
