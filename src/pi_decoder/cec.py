"""CEC TV control via cec-client subprocess."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time

log = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds — CEC bus init takes 2-3s
_CEC_OSD_NAME = "Pi-Decoder"

# Cached availability (checked once at startup, refreshed on demand)
_cec_available: bool | None = None


def is_available() -> bool:
    """Check if cec-client binary is on PATH."""
    global _cec_available
    if _cec_available is None:
        _cec_available = shutil.which("cec-client") is not None
        if not _cec_available:
            log.info("cec-client not found — CEC controls will be disabled")
    return _cec_available


async def configure_adapter() -> bool:
    """Register the kernel CEC adapter as a Playback device.

    Without a claimed logical address, Samsung Anynet+ rejects CEC messages
    as coming from "Unregistered". This registers LA 4 (Playback Device 1)
    with our OSD name so cec-client commands land on a valid identity.
    Safe to call repeatedly. Returns True on success.
    """
    if shutil.which("cec-ctl") is None:
        log.info("cec-ctl not found — skipping CEC adapter pre-registration")
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "cec-ctl", "--playback", "--osd-name", _CEC_OSD_NAME,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            log.info("CEC adapter registered as Playback device '%s'", _CEC_OSD_NAME)
            return True
        log.warning("cec-ctl registration failed: %s", stderr.decode(errors="replace").strip()[:200])
        return False
    except asyncio.TimeoutError:
        log.warning("cec-ctl registration timed out")
        return False
    except Exception as e:
        log.warning("cec-ctl registration error: %s", e)
        return False

# Cached power status to avoid spawning cec-client every 2s per WS client.
_power_cache: str = "unknown"
_power_cache_time: float = 0.0
_POWER_CACHE_TTL = 10.0  # seconds
# Serialises ALL cec-client invocations. Only one process can hold /dev/cec0
# at a time — concurrent calls get EBUSY (errno 16). This lock queues them.
_cec_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create the lock (must be done inside a running event loop)."""
    global _cec_lock
    if _cec_lock is None:
        _cec_lock = asyncio.Lock()
    return _cec_lock


async def _run_cec(command: str) -> str:
    """Pipe a command to cec-client and return stdout.

    Serialised by _cec_lock — concurrent subprocess invocations would collide
    on /dev/cec0 (EBUSY). Queued callers wait for their turn.
    """
    # cec-client re-claims the adapter's logical address and clears it on exit,
    # which undoes the volume path's Playback registration.
    _invalidate_audio_ready()
    lock = _get_lock()
    async with lock:
        proc = await asyncio.create_subprocess_exec(
            "cec-client", "-s", "-d", "1", "-o", _CEC_OSD_NAME,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=command.encode()),
                timeout=_TIMEOUT,
            )
            return stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"cec-client timed out after {_TIMEOUT}s")


_phys_addr: str | None = None  # Pi's HDMI physical address, detected once


async def _cec_ctl_registered(*args: str, timeout: float | None = None) -> tuple[bool, str]:
    """Run `cec-ctl --playback <args>` under the adapter lock; return (ok, stdout).

    The fast path for power / status / active-source: ~0.1-0.8s vs ~3-5s for
    cec-client (which re-inits the whole bus each call). Always registers as a
    Playback device first — an Unregistered cec-ctl is silently ignored by the
    TV/soundbar, and a cec-client scan (audio detection) wipes a prior
    registration, so we re-assert it every call.
    """
    cmd = ["sudo", "-n", "cec-ctl", "--playback", "--osd-name", _CEC_OSD_NAME, *args]
    lock = _get_lock()
    async with lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or _KEY_TIMEOUT)
            return proc.returncode == 0, stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            log.warning("cec-ctl %s timed out", args)
            return False, ""
        except Exception as e:
            log.warning("cec-ctl %s error: %s", args, e)
            return False, ""


async def _pi_phys_addr() -> str:
    """Pi's HDMI physical address (e.g. '2.0.0.0'), detected once and cached.

    Needed as the source address for the <Active Source> broadcast. Parsed from
    cec-ctl's own registration output via _register_playback().
    """
    global _phys_addr
    if _phys_addr is None:
        _phys_addr = await _register_playback()
    return _phys_addr


# ── Power ─────────────────────────────────────────────────────────

async def _invalidate_power_cache() -> None:
    """Force next get_power_status() to query the CEC bus."""
    global _power_cache_time
    _power_cache_time = 0.0


async def power_on() -> str:
    """Turn TV on via <Image View On> (fast cec-ctl path)."""
    ok, _ = await _cec_ctl_registered("--to", "0", "--image-view-on")
    log.info("CEC power on: %s", "sent" if ok else "FAILED")
    await _invalidate_power_cache()
    _invalidate_audio_ready()  # power change can drop System Audio Mode
    return "sent" if ok else "failed"


async def standby() -> str:
    """Put TV in standby (fast cec-ctl path)."""
    ok, _ = await _cec_ctl_registered("--to", "0", "--standby")
    log.info("CEC standby: %s", "sent" if ok else "FAILED")
    await _invalidate_power_cache()
    _invalidate_audio_ready()  # power change can drop System Audio Mode
    return "sent" if ok else "failed"


async def get_power_status() -> str:
    """Query TV power status with TTL cache.

    Returns 'on', 'standby', or 'unknown'.  Uses cec-ctl (~0.7s) instead of
    cec-client (~3.4s); only one query per TTL window regardless of how many
    WebSocket clients ask. Parses the REPORT_POWER_STATUS reply's pwr-state hex:
    0x00=on, 0x01=standby, 0x02=transition→on, 0x03=transition→standby.
    """
    global _power_cache, _power_cache_time

    now = time.monotonic()
    if now - _power_cache_time < _POWER_CACHE_TTL:
        return _power_cache

    status = "unknown"
    ok, output = await _cec_ctl_registered("--to", "0", "--give-device-power-status")
    if ok:
        for line in output.splitlines():
            low = line.lower()
            if "pwr-state" in low:
                if "0x00" in low or "0x02" in low:
                    status = "on"
                elif "0x01" in low or "0x03" in low:
                    status = "standby"
                break

    _power_cache = status
    _power_cache_time = time.monotonic()
    return _power_cache


async def toggle() -> str:
    """Toggle TV power based on current status.

    Returns the action taken: 'on' if the TV was turned on, 'standby' if it
    was put to standby. Treats 'unknown' as off and powers on, so a single
    button keeps working even when the bus can't report state. power_on() and
    standby() invalidate the status cache, so back-to-back presses stay in sync.
    """
    status = await get_power_status()
    if status == "on":
        await standby()
        return "standby"
    await power_on()
    return "on"


# ── Source / Input ────────────────────────────────────────────────

async def active_source() -> str:
    """Make the Pi the active HDMI source (fast cec-ctl path).

    Broadcasts <Active Source> for the Pi's physical address — the TV switches
    to the Pi's input. ~0.8s via cec-ctl vs ~3.8s via cec-client.
    """
    pa = await _pi_phys_addr()
    ok, _ = await _cec_ctl_registered("--active-source", f"phys-addr={pa}")
    log.info("CEC active source (%s): %s", pa, "sent" if ok else "FAILED")
    return "sent" if ok else "failed"


async def set_input(port: int) -> str:
    """Switch TV to HDMI port (1-4)."""
    if port < 1 or port > 4:
        raise ValueError(f"HDMI port must be 1-4, got {port}")
    output = await _run_cec(f"tx 4F:82:{port}0:00")
    log.info("CEC set input %d: %s", port, output.strip()[-80:])
    return output


# ── Volume / Keypress (fast path via cec-ctl) ─────────────────────
#
# Volume is sent to the AUDIO SYSTEM (logical address 5), NOT the TV. On this
# rig (Samsung TV + Sony HT-SF150 soundbar) the soundbar only honours CEC
# volume once it has been put into *System Audio Mode*, and only from a
# *registered* initiator. Sending volume key-presses to the TV (LA 0), or from
# an unregistered adapter, is silently ignored. So a volume burst needs:
#   1. the adapter registered as a Playback device (cec-ctl is "Unregistered"
#      by default);
#   2. the soundbar in System Audio Mode (<System Audio Mode Request> to LA 5);
#   3. the volume key-presses to LA 5.
# Steps 1+2 cost ~0.65s; the taps are ~0.15s each once armed. So we ARM ONCE and
# cache it (_arm_audio): repeat presses skip straight to the taps. The cache is
# invalidated when something can undo it — a cec-client scan wipes the LA, and a
# TV power change can drop System Audio Mode — plus a TTL backstop. cec-ctl is
# used throughout instead of cec-client (~3-5s bus init) for responsiveness.

_AUDIO_LA = "5"                  # CEC logical address of the audio system
_DEFAULT_PHYS_ADDR = "2.0.0.0"   # Pi's HDMI physical address (fallback only)
_AUDIO_READY_TTL = 20.0          # seconds the armed (registered + SAM) state is trusted
_audio_ready_until = 0.0         # monotonic deadline; 0 = must re-arm


def _invalidate_audio_ready() -> None:
    """Force the next volume burst to re-register and re-send the SAM request.

    Called whenever the armed state may have been undone: a cec-client scan
    clears the adapter's logical address, and a TV power change can drop the
    soundbar out of System Audio Mode.
    """
    global _audio_ready_until
    _audio_ready_until = 0.0

# cec-ctl --ui-cmd values for each key.
_UI_KEYS: dict[str, str] = {
    "volume-up": "volume-up",
    "volume-down": "volume-down",
    "mute": "mute",
}

_KEY_TIMEOUT = 3.0  # seconds per cec-ctl invocation
_TAP_GAP = 0.05     # seconds between press and release for a single tap


async def _run_cec_ctl(*args: str) -> bool:
    """Run `sudo -n cec-ctl <args>`. Fast path for keypress/release."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "cec-ctl", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_KEY_TIMEOUT)
        if proc.returncode != 0:
            log.debug("cec-ctl %s -> rc=%d: %s",
                      args, proc.returncode, stderr.decode(errors="replace").strip()[:120])
            return False
        return True
    except asyncio.TimeoutError:
        log.warning("cec-ctl %s timed out", args)
        return False
    except Exception as e:
        log.warning("cec-ctl %s error: %s", args, e)
        return False


async def _register_playback() -> str:
    """Register the adapter as a Playback device; return its physical address.

    cec-ctl runs "Unregistered" by default and volume key-presses from an
    unregistered initiator are ignored. Registering as Playback (LA 4) is cheap
    and re-establishes the LA after a cec-client scan (audio detection) has
    cleared it. The physical address is parsed from the same call so the System
    Audio Mode request can name the Pi as the audio source.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "cec-ctl", "--playback", "--osd-name", _CEC_OSD_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_KEY_TIMEOUT)
    except Exception as e:
        log.warning("cec-ctl --playback failed: %s", e)
        return _DEFAULT_PHYS_ADDR
    for line in stdout.decode(errors="replace").splitlines():
        if "Physical Address" in line:
            pa = line.split(":", 1)[1].strip()
            if pa[:1].isdigit():
                return pa
    return _DEFAULT_PHYS_ADDR


async def _arm_audio() -> None:
    """Ensure the adapter is registered and the soundbar is in System Audio Mode.

    Cheap to call repeatedly: it re-arms at most once per _AUDIO_READY_TTL (or
    after an explicit _invalidate_audio_ready()), otherwise it's a no-op. Must be
    called with the CEC lock held.
    """
    global _audio_ready_until
    if time.monotonic() < _audio_ready_until:
        return
    phys = await _register_playback()
    # Put the soundbar into System Audio Mode (idempotent; it stays on).
    await _run_cec_ctl("--to", _AUDIO_LA, "--system-audio-mode-request",
                       f"phys-addr={phys}")
    _audio_ready_until = time.monotonic() + _AUDIO_READY_TTL


async def _audio_key_burst(key: str, steps: int) -> dict:
    """Send `steps` volume key-presses to the audio system (LA 5).

    Arms the audio path (register + System Audio Mode, cached) then taps — see
    the module comment for why those are needed. 'Fire now or drop': if another
    CEC call is in flight we drop rather than queue, so a held button can't keep
    changing volume after the user stops. Returns {"ok", "sent", "dropped"}.
    """
    if key not in _UI_KEYS:
        raise ValueError(f"Unknown key: {key!r}")
    if steps < 1:
        steps = 1
    if steps > 20:
        steps = 20  # safety cap
    if _is_busy():
        return {"ok": True, "sent": 0, "dropped": True}
    ui_cmd = _UI_KEYS[key]
    lock = _get_lock()
    async with lock:
        await _arm_audio()
        sent = 0
        for _ in range(steps):
            if not await _run_cec_ctl("--to", _AUDIO_LA,
                                      "--user-control-pressed", f"ui-cmd={ui_cmd}"):
                break
            await asyncio.sleep(_TAP_GAP)
            await _run_cec_ctl("--to", _AUDIO_LA, "--user-control-released")
            sent += 1
    return {"ok": sent > 0, "sent": sent, "dropped": False}


def _is_busy() -> bool:
    """True if the CEC adapter is currently held by another coroutine."""
    lock = _get_lock()
    return lock.locked()


async def volume_up(steps: int = 1) -> dict:
    """Raise volume `steps` times via the soundbar. Drops if the adapter is busy.

    Returns {"ok": bool, "sent": int, "dropped": bool}.
    """
    return await _audio_key_burst("volume-up", steps)


async def volume_down(steps: int = 1) -> dict:
    """Lower volume `steps` times via the soundbar. Drops if the adapter is busy."""
    return await _audio_key_burst("volume-down", steps)


async def mute() -> dict:
    """Toggle mute on the soundbar. Drops if the adapter is busy."""
    return await _audio_key_burst("mute", 1)


# ── Audio System Detection & Routing ───────────────────────────────
#
# Samsung Anynet+ and most HDMI-CEC AVRs/soundbars implement <System Audio
# Mode Request> (opcode 0x70). Playback devices (us, LA 4) can ASK the TV
# to route audio to an audio system at a given physical address. Samsung
# often replies with Feature Abort from a playback device, but:
#   - We can still reliably READ the current mode by asking the soundbar
#   - On non-Samsung TVs the request is honoured
#   - The read value is useful for the web UI and Companion status
#
# Soundbar PA re-parsed from `cec-client scan` output each time instead
# of hardcoding, so moving the soundbar between TV HDMI ports still works.


async def detect_audio_system(timeout: float = 15.0) -> dict | None:
    """Scan the CEC bus and return info about an Audio System (LA 5), or None.

    Returns a dict with keys: {"logical_addr", "phys_addr", "vendor", "osd"}.
    phys_addr is the integer form (e.g. 0x3000 for "3.0.0.0").
    """
    # The cec-client scan re-claims the adapter's logical address, undoing the
    # volume path's Playback registration — force a re-arm on the next burst.
    _invalidate_audio_ready()
    lock = _get_lock()
    async with lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                "cec-client", "-s", "-d", "1", "-o", _CEC_OSD_NAME,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=b"scan"),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("detect_audio_system: cec-client scan timed out after %ss", timeout)
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return None
        except Exception as e:
            log.warning("detect_audio_system: scan failed: %r", e)
            return None

    text = stdout.decode(errors="replace")
    # Walk the scan output looking for "device #5: Audio" block
    block = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("device #5"):
            block = {}
            continue
        if block is None:
            continue
        if stripped.startswith("device #"):
            break  # end of Audio block
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            block[key.strip().lower()] = val.strip()

    if not block:
        return None

    pa_str = block.get("address", "")
    # "3.0.0.0" -> 0x3000
    try:
        parts = [int(p) & 0xF for p in pa_str.split(".")]
        while len(parts) < 4:
            parts.append(0)
        phys_addr = (parts[0] << 12) | (parts[1] << 8) | (parts[2] << 4) | parts[3]
    except Exception:
        phys_addr = None

    return {
        "logical_addr": 5,
        "phys_addr": phys_addr,
        "phys_addr_str": pa_str or None,
        "vendor": block.get("vendor"),
        "osd": block.get("osd string"),
    }


async def get_system_audio_mode() -> str:
    """Query the soundbar for System Audio Mode status.

    Returns 'on', 'off', or 'unknown'. 'unknown' means no audio system was
    reachable or the adapter call failed.
    """
    if shutil.which("cec-ctl") is None:
        return "unknown"
    lock = _get_lock()
    async with lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "cec-ctl",
                "--playback", "--osd-name", _CEC_OSD_NAME,
                "--to", "5", "--give-system-audio-mode-status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception as e:
            log.debug("get_system_audio_mode: %s", e)
            return "unknown"
    text = stdout.decode(errors="replace")
    if "sys-aud-status: on" in text:
        return "on"
    if "sys-aud-status: off" in text:
        return "off"
    return "unknown"


async def request_system_audio_mode(phys_addr: int, enable: bool) -> bool:
    """Send <System Audio Mode Request> to the TV.

    phys_addr: integer form (e.g. 0x3000 for soundbar at 3.0.0.0). Ignored if
    enable=False (we send 0xFFFF to disable).
    Returns True if the message was transmitted OK (regardless of whether the
    TV honoured or aborted it).
    """
    if shutil.which("cec-ctl") is None:
        return False
    pa = phys_addr if enable else 0xFFFF
    pa_arg = f"phys-addr=0x{pa:04x}"
    lock = _get_lock()
    async with lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "cec-ctl",
                "--playback", "--osd-name", _CEC_OSD_NAME,
                "--to", "0", "--system-audio-mode-request", pa_arg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception as e:
            log.warning("request_system_audio_mode failed: %s", e)
            return False
    text = stdout.decode(errors="replace")
    # Transmit outcome is logged as "Tx, OK" — the Rx may still Feature Abort.
    return "Tx, OK" in text


async def ensure_audio_system_preferred(config) -> dict:
    """Startup helper: if the config toggle is on and an audio system is on
    the bus, make sure SAM is on (audio routed to soundbar). Best-effort.

    Returns a dict suitable for logging:
      {"enabled": bool, "audio_system": dict|None, "current": str, "action": str}
    action is one of: "disabled", "no-audio-system", "already-on",
    "requested-on", "request-failed".
    """
    result: dict = {
        "enabled": bool(getattr(config.cec, "prefer_audio_system", True)),
        "audio_system": None,
        "current": "unknown",
        "action": "disabled",
    }
    if not result["enabled"]:
        return result
    audio = await detect_audio_system()
    result["audio_system"] = audio
    if not audio:
        result["action"] = "no-audio-system"
        return result
    current = await get_system_audio_mode()
    result["current"] = current
    if current == "on":
        result["action"] = "already-on"
        return result
    # Try to enable — Samsung may refuse but we at least try.
    pa = audio.get("phys_addr") or 0x3000
    ok = await request_system_audio_mode(pa, enable=True)
    result["action"] = "requested-on" if ok else "request-failed"
    return result
