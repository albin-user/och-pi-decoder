"""HDMI display resolution management — read DRM modes, parse/update cmdline.txt."""

from __future__ import annotations

import asyncio
import logging
import platform
import re
from pathlib import Path

log = logging.getLogger(__name__)

_CMDLINE_PATHS = [Path("/boot/firmware/cmdline.txt"), Path("/boot/cmdline.txt")]
_DRM_MODES_GLOB = "/sys/class/drm/card*-HDMI-A-1/modes"

_FALLBACK_MODES = ["1920x1080", "1280x720", "720x480"]

_ALL_RATES = [24, 25, 30, 50, 60]
_4K_RATES_PI4 = [24, 25, 30]
_4K_RATES_PI5 = [24, 25, 30, 50, 60]

_PI_MODEL_PATH = Path("/proc/device-tree/model")


def get_pi_model() -> int:
    """Detect the Raspberry Pi model number (4, 5, etc.).

    Reads /proc/device-tree/model and parses "Raspberry Pi N".
    Falls back to 4 (most restrictive) on failure.
    """
    try:
        text = _PI_MODEL_PATH.read_text().strip().rstrip("\x00")
        match = re.search(r'Raspberry Pi (\d+)', text)
        if match:
            return int(match.group(1))
    except Exception:
        log.debug("Could not detect Pi model, defaulting to 4", exc_info=True)
    return 4


def get_refresh_rates_for_resolution(resolution: str, pi_model: int | None = None) -> list[int]:
    """Return available refresh rates for a given resolution.

    For 4K (>= 3840x2160) on Pi 4, rates are limited to 24/25/30 Hz.
    Pi 5+ supports all rates at 4K. All other resolutions get all rates.
    """
    if pi_model is None:
        pi_model = get_pi_model()

    # Check if this is a 4K+ resolution
    m = re.match(r'^(\d+)x(\d+)', resolution)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w >= 3840 and h >= 2160:
            if pi_model <= 4:
                return list(_4K_RATES_PI4)
            return list(_4K_RATES_PI5)

    return list(_ALL_RATES)


def get_available_modes() -> list[str]:
    """Read available HDMI modes from DRM sysfs.

    Returns deduplicated list like ["1920x1080", "1280x720", ...].
    Falls back to a default list on non-Linux or missing sysfs.
    """
    if platform.system() != "Linux":
        return list(_FALLBACK_MODES)

    import glob as _glob
    mode_files = sorted(_glob.glob(_DRM_MODES_GLOB))
    if not mode_files:
        return list(_FALLBACK_MODES)

    seen: set[str] = set()
    modes: list[str] = []
    for mf in mode_files:
        try:
            text = Path(mf).read_text()
            for line in text.strip().splitlines():
                # Lines look like "1920x1080" or "1920x1080i"
                res = line.strip()
                # Normalize: strip trailing 'i' or 'p' suffix
                res = re.sub(r'[ip]$', '', res)
                if res and res not in seen:
                    seen.add(res)
                    modes.append(res)
        except Exception:
            log.debug("Failed to read DRM modes from %s", mf, exc_info=True)

    return modes if modes else list(_FALLBACK_MODES)


def _find_cmdline_path() -> Path | None:
    """Find the first existing cmdline.txt path."""
    for p in _CMDLINE_PATHS:
        if p.exists():
            return p
    return None


def get_current_resolution() -> str:
    """Parse the current video= parameter from cmdline.txt.

    Returns string like "1920x1080@60D" or "" if not found.
    """
    cmdline_path = _find_cmdline_path()
    if not cmdline_path:
        return ""

    try:
        content = cmdline_path.read_text().strip()
        # Look for video=HDMI-A-1:WxH@RD or similar
        match = re.search(r'video=HDMI-A-1:(\S+)', content)
        if match:
            return match.group(1)
    except Exception:
        log.debug("Failed to read cmdline.txt", exc_info=True)

    return ""


async def set_display_resolution(resolution: str) -> None:
    """Write updated cmdline.txt with new video= parameter via sudo tee.

    Strips old video=HDMI-A-1:... param and appends new one.
    """
    if platform.system() != "Linux":
        log.debug("Display resolution change skipped (not Linux)")
        return

    cmdline_path = _find_cmdline_path()
    if not cmdline_path:
        raise FileNotFoundError("cmdline.txt not found")

    content = cmdline_path.read_text().strip()

    # Remove existing video=HDMI-A-1:... parameter
    content = re.sub(r'\s*video=HDMI-A-1:\S+', '', content)

    # Append new parameter
    content = content.strip() + f" video=HDMI-A-1:{resolution}"

    proc = await asyncio.create_subprocess_exec(
        "sudo", "tee", str(cmdline_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(input=content.encode()), timeout=10)
    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Failed to write cmdline.txt (rc={proc.returncode}): {err}")

    log.info("HDMI resolution set to %s in %s", resolution, cmdline_path)
