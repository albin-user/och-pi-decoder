"""CEC TV control via cec-client subprocess."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds — CEC bus init takes 2-3s


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

async def power_on() -> str:
    """Turn TV on (address 0 = TV)."""
    output = await _run_cec("on 0")
    log.info("CEC power on: %s", output.strip()[-80:])
    return output


async def standby() -> str:
    """Put TV in standby."""
    output = await _run_cec("standby 0")
    log.info("CEC standby: %s", output.strip()[-80:])
    return output


async def get_power_status() -> str:
    """Query TV power status. Returns 'on', 'standby', or 'unknown'."""
    output = await _run_cec("pow 0")
    lower = output.lower()
    if "power status: on" in lower:
        return "on"
    if "power status: standby" in lower:
        return "standby"
    return "unknown"


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
