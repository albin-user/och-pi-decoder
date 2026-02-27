"""Filesystem utilities — read-only root protection for SD card longevity."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
from contextlib import contextmanager

log = logging.getLogger(__name__)

_lock = threading.Lock()
_refcounts: dict[str, int] = {}


@contextmanager
def writable(mount_point: str = "/"):
    """Context manager that remounts a filesystem read-write, then back to read-only.

    Reference-counted per mount point so nested/concurrent calls are safe —
    only the outermost call performs the actual mount operations.

    No-op on non-Linux (macOS dev machines).
    """
    if platform.system() != "Linux":
        yield
        return

    with _lock:
        _refcounts[mount_point] = _refcounts.get(mount_point, 0) + 1
        needs_mount = _refcounts[mount_point] == 1

    if needs_mount:
        result = subprocess.run(
            ["sudo", "mount", "-o", "remount,rw", mount_point],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            with _lock:
                _refcounts[mount_point] -= 1
            raise RuntimeError(
                f"Failed to remount {mount_point} rw: {result.stderr.strip()}"
            )

    try:
        yield
    finally:
        with _lock:
            _refcounts[mount_point] -= 1
            needs_unmount = _refcounts[mount_point] == 0

        if needs_unmount:
            try:
                os.sync()
                result = subprocess.run(
                    ["sudo", "mount", "-o", "remount,ro", mount_point],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    log.warning(
                        "Failed to remount %s ro: %s",
                        mount_point, result.stderr.strip(),
                    )
            except Exception:
                log.warning("Failed to remount %s ro", mount_point, exc_info=True)
