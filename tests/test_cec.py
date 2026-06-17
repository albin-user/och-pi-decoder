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
        with patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(True, "")) as mock_ctl:
            result = await cec.power_on()
        assert result == "sent"
        assert "--image-view-on" in mock_ctl.call_args.args

    @pytest.mark.asyncio
    async def test_standby(self):
        with patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(True, "")) as mock_ctl:
            result = await cec.standby()
        assert result == "sent"
        assert "--standby" in mock_ctl.call_args.args

    @pytest.mark.asyncio
    async def test_toggle_on_when_off(self):
        """toggle() powers on when the TV is in standby."""
        with patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock,
                   return_value="standby"), \
             patch("pi_decoder.cec.power_on", new_callable=AsyncMock) as p_on, \
             patch("pi_decoder.cec.standby", new_callable=AsyncMock) as p_sb:
            result = await cec.toggle()
        assert result == "on"
        p_on.assert_awaited_once()
        p_sb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_toggle_standby_when_on(self):
        """toggle() puts the TV in standby when it is on."""
        with patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock,
                   return_value="on"), \
             patch("pi_decoder.cec.power_on", new_callable=AsyncMock) as p_on, \
             patch("pi_decoder.cec.standby", new_callable=AsyncMock) as p_sb:
            result = await cec.toggle()
        assert result == "standby"
        p_sb.assert_awaited_once()
        p_on.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_toggle_unknown_powers_on(self):
        """toggle() treats 'unknown' as off and powers on."""
        with patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock,
                   return_value="unknown"), \
             patch("pi_decoder.cec.power_on", new_callable=AsyncMock) as p_on, \
             patch("pi_decoder.cec.standby", new_callable=AsyncMock) as p_sb:
            result = await cec.toggle()
        assert result == "on"
        p_on.assert_awaited_once()
        p_sb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_power_status_on(self):
        with patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(True, "\tpwr-state: on (0x00)\n")):
            result = await cec.get_power_status()
        assert result == "on"

    @pytest.mark.asyncio
    async def test_get_power_status_standby(self):
        with patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(True, "\tpwr-state: standby (0x01)\n")):
            result = await cec.get_power_status()
        assert result == "standby"

    @pytest.mark.asyncio
    async def test_get_power_status_transition_to_on(self):
        """pwr-state 0x02 (transition to on) reads as 'on'."""
        with patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(True, "\tpwr-state: in transition standby to on (0x02)\n")):
            result = await cec.get_power_status()
        assert result == "on"

    @pytest.mark.asyncio
    async def test_get_power_status_unknown(self):
        with patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(True, "some garbage output\n")):
            result = await cec.get_power_status()
        assert result == "unknown"

    @pytest.mark.asyncio
    async def test_get_power_status_uses_cache(self):
        """Second call within TTL returns cached value without re-querying."""
        mock = AsyncMock(return_value=(True, "\tpwr-state: on (0x00)\n"))
        with patch("pi_decoder.cec._cec_ctl_registered", mock):
            result1 = await cec.get_power_status()
            result2 = await cec.get_power_status()
        assert result1 == result2 == "on"
        mock.assert_called_once()  # Only one query

    @pytest.mark.asyncio
    async def test_get_power_status_cache_expires(self):
        """After TTL expires, a fresh query is made."""
        mock = AsyncMock(side_effect=[
            (True, "\tpwr-state: on (0x00)\n"),
            (True, "\tpwr-state: standby (0x01)\n"),
        ])
        with patch("pi_decoder.cec._cec_ctl_registered", mock):
            result1 = await cec.get_power_status()
            assert result1 == "on"
            # Force cache expiry
            cec._power_cache_time = time.monotonic() - cec._POWER_CACHE_TTL - 1
            result2 = await cec.get_power_status()
            assert result2 == "standby"
        assert mock.call_count == 2

    @pytest.mark.asyncio
    async def test_get_power_status_error_returns_unknown(self):
        """If the cec-ctl query fails (ok=False), return 'unknown'."""
        with patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(False, "")):
            result = await cec.get_power_status()
        assert result == "unknown"

    @pytest.mark.asyncio
    async def test_power_on_invalidates_cache(self):
        """power_on() should invalidate cache so next status query is fresh."""
        mock = AsyncMock(return_value=(True, "\tpwr-state: standby (0x01)\n"))
        with patch("pi_decoder.cec._cec_ctl_registered", mock):
            # Prime the cache
            await cec.get_power_status()
            assert cec._power_cache == "standby"
            await cec.power_on()
            assert cec._power_cache_time == 0.0

    @pytest.mark.asyncio
    async def test_standby_invalidates_cache(self):
        """standby() should invalidate cache so next status query is fresh."""
        mock = AsyncMock(return_value=(True, "\tpwr-state: on (0x00)\n"))
        with patch("pi_decoder.cec._cec_ctl_registered", mock):
            await cec.get_power_status()
            assert cec._power_cache == "on"
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
        with patch("pi_decoder.cec._pi_phys_addr", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._cec_ctl_registered", new_callable=AsyncMock,
                   return_value=(True, "")) as mock_ctl:
            result = await cec.active_source()
        assert result == "sent"
        assert "--active-source" in mock_ctl.call_args.args
        assert "phys-addr=2.0.0.0" in mock_ctl.call_args.args

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


def _pressed_calls(mock_ctl):
    """cec-ctl calls that are a <User Control Pressed>."""
    return [c for c in mock_ctl.call_args_list if "--user-control-pressed" in c.args]


class TestVolumeCommands:
    """Volume / mute go to the audio system (LA 5) under System Audio Mode."""

    def setup_method(self):
        cec._cec_lock = None

    @pytest.mark.asyncio
    async def test_volume_up_default_1_step(self):
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, return_value=True) as mock_ctl:
            result = await cec.volume_up()
        assert result == {"ok": True, "sent": 1, "dropped": False}
        pressed = _pressed_calls(mock_ctl)
        assert len(pressed) == 1
        assert "ui-cmd=volume-up" in pressed[0].args
        assert "5" in pressed[0].args  # sent to the audio system (LA 5)

    @pytest.mark.asyncio
    async def test_sends_system_audio_mode_request_first(self):
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, return_value=True) as mock_ctl:
            await cec.volume_up()
        sam = [c for c in mock_ctl.call_args_list if "--system-audio-mode-request" in c.args]
        assert len(sam) == 1
        assert "phys-addr=2.0.0.0" in sam[0].args

    @pytest.mark.asyncio
    async def test_volume_up_multiple_steps(self):
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, return_value=True) as mock_ctl:
            result = await cec.volume_up(steps=5)
        assert result == {"ok": True, "sent": 5, "dropped": False}
        assert len(_pressed_calls(mock_ctl)) == 5

    @pytest.mark.asyncio
    async def test_volume_up_clamped_to_max(self):
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, return_value=True) as mock_ctl:
            await cec.volume_up(steps=999)
        assert len(_pressed_calls(mock_ctl)) == 20  # safety cap

    @pytest.mark.asyncio
    async def test_volume_up_clamped_to_min(self):
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, return_value=True) as mock_ctl:
            result = await cec.volume_up(steps=0)
        assert result["sent"] == 1
        assert len(_pressed_calls(mock_ctl)) == 1

    @pytest.mark.asyncio
    async def test_volume_down(self):
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, return_value=True) as mock_ctl:
            result = await cec.volume_down(steps=3)
        assert result == {"ok": True, "sent": 3, "dropped": False}
        for c in _pressed_calls(mock_ctl):
            assert "ui-cmd=volume-down" in c.args

    @pytest.mark.asyncio
    async def test_mute(self):
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, return_value=True) as mock_ctl:
            result = await cec.mute()
        assert result == {"ok": True, "sent": 1, "dropped": False}
        pressed = _pressed_calls(mock_ctl)
        assert len(pressed) == 1
        assert "ui-cmd=mute" in pressed[0].args

    @pytest.mark.asyncio
    async def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="Unknown key"):
            await cec._audio_key_burst("bogus", 1)

    @pytest.mark.asyncio
    async def test_drops_when_busy(self):
        """If the adapter lock is held, volume commands drop instead of queueing."""
        with patch("pi_decoder.cec._is_busy", return_value=True), \
             patch("pi_decoder.cec._register_playback", new_callable=AsyncMock) as mock_reg, \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock) as mock_ctl:
            result = await cec.volume_up(steps=3)
        assert result == {"ok": True, "sent": 0, "dropped": True}
        mock_reg.assert_not_called()
        mock_ctl.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_on_tap_failure(self):
        """If a press fails mid-burst, stop and report how many succeeded."""
        # calls: SAM request, press1, release1, press2, release2, press3(fail)
        side_effects = [True, True, True, True, True, False]
        with patch("pi_decoder.cec._register_playback", new_callable=AsyncMock, return_value="2.0.0.0"), \
             patch("pi_decoder.cec._run_cec_ctl", new_callable=AsyncMock, side_effect=side_effects):
            result = await cec.volume_up(steps=4)
        assert result["sent"] == 2  # stops after the 3rd press fails


class TestRegisterPlayback:
    """_register_playback parses the adapter's physical address from cec-ctl."""

    def setup_method(self):
        cec._cec_lock = None

    @pytest.mark.asyncio
    async def test_parses_phys_addr(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(b"\tPhysical Address           : 2.0.0.0\n", b""))
        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            pa = await cec._register_playback()
        assert pa == "2.0.0.0"

    @pytest.mark.asyncio
    async def test_falls_back_on_error(self):
        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", side_effect=OSError("boom")):
            pa = await cec._register_playback()
        assert pa == cec._DEFAULT_PHYS_ADDR


class TestAudioSystemDetection:
    """Audio routing helpers: detect_audio_system, get_system_audio_mode,
    request_system_audio_mode, ensure_audio_system_preferred."""

    def setup_method(self):
        cec._cec_lock = None

    @pytest.mark.asyncio
    async def test_detect_audio_system_parses_scan_output(self):
        scan_output = (
            "device #0: TV\n"
            "address:       0.0.0.0\n"
            "vendor:        Samsung\n"
            "device #1: Recorder 1\n"
            "address:       2.0.0.0\n"
            "device #5: Audio\n"
            "address:       3.0.0.0\n"
            "vendor:        Sony\n"
            "osd string:    HT-SF150\n"
            "currently active source: unknown (-1)\n"
        )
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(scan_output.encode(), b""))
        proc.returncode = 0
        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            audio = await cec.detect_audio_system()
        assert audio is not None
        assert audio["logical_addr"] == 5
        assert audio["phys_addr"] == 0x3000
        assert audio["phys_addr_str"] == "3.0.0.0"
        assert audio["vendor"] == "Sony"
        assert audio["osd"] == "HT-SF150"

    @pytest.mark.asyncio
    async def test_detect_audio_system_none_when_no_device_5(self):
        scan_output = (
            "device #0: TV\n"
            "address:       0.0.0.0\n"
            "device #1: Recorder 1\n"
            "address:       2.0.0.0\n"
        )
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(scan_output.encode(), b""))
        proc.returncode = 0
        with patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            audio = await cec.detect_audio_system()
        assert audio is None

    @pytest.mark.asyncio
    async def test_get_system_audio_mode_on(self):
        out = b"    Received from Audio System (5):\n\tsys-aud-status: on (0x01)\n"
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(out, b""))
        proc.returncode = 0
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            mode = await cec.get_system_audio_mode()
        assert mode == "on"

    @pytest.mark.asyncio
    async def test_get_system_audio_mode_off(self):
        out = b"    Received from Audio System (5):\n\tsys-aud-status: off (0x00)\n"
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(out, b""))
        proc.returncode = 0
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            mode = await cec.get_system_audio_mode()
        assert mode == "off"

    @pytest.mark.asyncio
    async def test_get_system_audio_mode_unknown_when_no_response(self):
        out = b"    Received: nothing matching\n"
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(out, b""))
        proc.returncode = 0
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc):
            mode = await cec.get_system_audio_mode()
        assert mode == "unknown"

    @pytest.mark.asyncio
    async def test_get_system_audio_mode_no_cec_ctl(self):
        with patch("pi_decoder.cec.shutil.which", return_value=None):
            mode = await cec.get_system_audio_mode()
        assert mode == "unknown"

    @pytest.mark.asyncio
    async def test_request_system_audio_mode_enable(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"Transmit: Tx, OK", b""))
        proc.returncode = 0
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            ok = await cec.request_system_audio_mode(0x3000, enable=True)
        assert ok is True
        args = mock_exec.call_args[0]
        assert "--system-audio-mode-request" in args
        assert "phys-addr=0x3000" in args

    @pytest.mark.asyncio
    async def test_request_system_audio_mode_disable_sends_ffff(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"Transmit: Tx, OK", b""))
        proc.returncode = 0
        with patch("pi_decoder.cec.shutil.which", return_value="/usr/bin/cec-ctl"), \
             patch("pi_decoder.cec.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            ok = await cec.request_system_audio_mode(0x3000, enable=False)
        assert ok is True
        args = mock_exec.call_args[0]
        assert "phys-addr=0xffff" in args

    @pytest.mark.asyncio
    async def test_ensure_audio_system_preferred_disabled(self):
        """If config toggle is off, do nothing."""
        class FakeCfg:
            class cec:
                prefer_audio_system = False
        result = await cec.ensure_audio_system_preferred(FakeCfg())
        assert result["enabled"] is False
        assert result["action"] == "disabled"

    @pytest.mark.asyncio
    async def test_ensure_audio_system_preferred_no_audio_system(self):
        """No audio system on bus → skip with clear action."""
        class FakeCfg:
            class cec:
                prefer_audio_system = True
        with patch("pi_decoder.cec.detect_audio_system", new_callable=AsyncMock, return_value=None):
            result = await cec.ensure_audio_system_preferred(FakeCfg())
        assert result["action"] == "no-audio-system"

    @pytest.mark.asyncio
    async def test_ensure_audio_system_preferred_already_on(self):
        """Audio system detected and SAM already on → no further action."""
        class FakeCfg:
            class cec:
                prefer_audio_system = True
        audio = {"logical_addr": 5, "phys_addr": 0x3000, "vendor": "Sony"}
        with patch("pi_decoder.cec.detect_audio_system", new_callable=AsyncMock, return_value=audio), \
             patch("pi_decoder.cec.get_system_audio_mode", new_callable=AsyncMock, return_value="on"):
            result = await cec.ensure_audio_system_preferred(FakeCfg())
        assert result["action"] == "already-on"
        assert result["current"] == "on"

    @pytest.mark.asyncio
    async def test_ensure_audio_system_preferred_requests_on(self):
        """SAM off → best-effort request enable."""
        class FakeCfg:
            class cec:
                prefer_audio_system = True
        audio = {"logical_addr": 5, "phys_addr": 0x3000, "vendor": "Sony"}
        with patch("pi_decoder.cec.detect_audio_system", new_callable=AsyncMock, return_value=audio), \
             patch("pi_decoder.cec.get_system_audio_mode", new_callable=AsyncMock, return_value="off"), \
             patch("pi_decoder.cec.request_system_audio_mode", new_callable=AsyncMock, return_value=True) as mock_req:
            result = await cec.ensure_audio_system_preferred(FakeCfg())
        assert result["action"] == "requested-on"
        mock_req.assert_awaited_once_with(0x3000, enable=True)

    @pytest.mark.asyncio
    async def test_ensure_audio_system_preferred_request_failed(self):
        class FakeCfg:
            class cec:
                prefer_audio_system = True
        audio = {"logical_addr": 5, "phys_addr": 0x3000}
        with patch("pi_decoder.cec.detect_audio_system", new_callable=AsyncMock, return_value=audio), \
             patch("pi_decoder.cec.get_system_audio_mode", new_callable=AsyncMock, return_value="off"), \
             patch("pi_decoder.cec.request_system_audio_mode", new_callable=AsyncMock, return_value=False):
            result = await cec.ensure_audio_system_preferred(FakeCfg())
        assert result["action"] == "request-failed"
