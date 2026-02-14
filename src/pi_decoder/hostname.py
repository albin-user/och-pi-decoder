"""Hostname sync — keep system hostname in sync with pi-decoder name."""

from __future__ import annotations

import asyncio
import logging
import platform
import re
from pathlib import Path

log = logging.getLogger(__name__)


def sanitize_hostname(name: str) -> str:
    """Convert a display name to a valid hostname.

    - Lowercase
    - Replace spaces and underscores with hyphens
    - Strip non-alphanumeric/non-hyphen characters
    - Collapse consecutive hyphens
    - Strip leading/trailing hyphens
    - Truncate to 63 chars (RFC 1123)
    - Fallback to "pi-decoder" if result is empty
    """
    h = name.lower()
    h = h.replace(" ", "-").replace("_", "-")
    h = re.sub(r"[^a-z0-9\-]", "", h)
    h = re.sub(r"-{2,}", "-", h)
    h = h.strip("-")
    h = h[:63]
    return h or "pi-decoder"


async def set_hostname(name: str) -> str:
    """Sanitize name, then set system hostname + update /etc/hosts.

    Runs: sudo hostnamectl set-hostname <sanitized>
    Updates /etc/hosts: replaces 127.0.1.1 line
    Returns the sanitized hostname that was set.
    Logs warnings on failure (non-Linux, no sudo, etc.) but doesn't raise.
    """
    hostname = sanitize_hostname(name)

    if platform.system() != "Linux":
        log.debug("Hostname sync skipped (not Linux)")
        return hostname

    # Set hostname via hostnamectl
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "hostnamectl", "set-hostname", hostname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            log.warning("hostnamectl failed (rc=%d): %s", proc.returncode, err)
            return hostname
    except asyncio.TimeoutError:
        log.warning("hostnamectl timed out")
        return hostname
    except Exception:
        log.warning("hostnamectl failed", exc_info=True)
        return hostname

    # Update /etc/hosts — replace or add 127.0.1.1 line (safe Python file write)
    try:
        hosts_path = Path("/etc/hosts")
        lines = hosts_path.read_text().splitlines()
        new_lines = []
        found = False
        for line in lines:
            if line.strip().startswith("127.0.1.1"):
                new_lines.append(f"127.0.1.1\t{hostname}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"127.0.1.1\t{hostname}")
        content = "\n".join(new_lines) + "\n"
        # Write via sudo tee to handle permissions
        proc = await asyncio.create_subprocess_exec(
            "sudo", "tee", "/etc/hosts",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(input=content.encode()), timeout=10)
    except Exception:
        log.warning("Failed to update /etc/hosts", exc_info=True)

    log.info("System hostname set to '%s'", hostname)
    return hostname
