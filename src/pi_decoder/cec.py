"""CEC TV control via cec-client subprocess."""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds — CEC bus init takes 2-3s

# Cached power status to avoid spawning cec-client every 2s per WS client.
_power_cache: str = "unknown"
_power_cache_time: float = 0.0
_POWER_CACHE_TTL = 10.0  # seconds
_power_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazily create the lock (must be done inside a running event loop)."""
    global _power_lock
    if _power_lock is None:
        _power_lock = asyncio.Lock()
    return _power_lock


async def _run_cec(command: str) -> str:
    """Pipe a command to cec-client and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "cec-client", "-s", "-d", "1",
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


# ── Power ─────────────────────────────────────────────────────────

async def _invalidate_power_cache() -> None:
    """Force next get_power_status() to query the CEC bus."""
    global _power_cache_time
    _power_cache_time = 0.0


async def power_on() -> str:
    """Turn TV on (address 0 = TV)."""
    output = await _run_cec("on 0")
    log.info("CEC power on: %s", output.strip()[-80:])
    await _invalidate_power_cache()
    return output


async def standby() -> str:
    """Put TV in standby."""
    output = await _run_cec("standby 0")
    log.info("CEC standby: %s", output.strip()[-80:])
    await _invalidate_power_cache()
    return output


async def get_power_status() -> str:
    """Query TV power status with TTL cache.

    Returns 'on', 'standby', or 'unknown'.  Only spawns one cec-client
    process per TTL window regardless of how many WebSocket clients ask.
    """
    global _power_cache, _power_cache_time

    now = time.monotonic()
    if now - _power_cache_time < _POWER_CACHE_TTL:
        return _power_cache

    lock = _get_lock()
    async with lock:
        # Re-check after acquiring lock (another coroutine may have refreshed)
        now = time.monotonic()
        if now - _power_cache_time < _POWER_CACHE_TTL:
            return _power_cache

        try:
            output = await _run_cec("pow 0")
            lower = output.lower()
            if "power status: on" in lower:
                status = "on"
            elif "power status: standby" in lower:
                status = "standby"
            else:
                status = "unknown"
        except Exception:
            status = "unknown"

        _power_cache = status
        _power_cache_time = time.monotonic()
        return _power_cache


# ── Source / Input ────────────────────────────────────────────────

async def active_source() -> str:
    """Make Pi the active HDMI source."""
    output = await _run_cec("as")
    log.info("CEC active source: %s", output.strip()[-80:])
    return output


async def set_input(port: int) -> str:
    """Switch TV to HDMI port (1-4)."""
    if port < 1 or port > 4:
        raise ValueError(f"HDMI port must be 1-4, got {port}")
    output = await _run_cec(f"tx 4F:82:{port}0:00")
    log.info("CEC set input %d: %s", port, output.strip()[-80:])
    return output


# ── Volume ────────────────────────────────────────────────────────

async def volume_up() -> str:
    output = await _run_cec("volup 0")
    return output


async def volume_down() -> str:
    output = await _run_cec("voldown 0")
    return output


async def mute() -> str:
    output = await _run_cec("mute 0")
    return output
