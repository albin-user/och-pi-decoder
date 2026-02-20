"""mpv process lifecycle and JSON IPC client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from importlib.metadata import version as pkg_version
from pathlib import Path

from pi_decoder.config import Config

log = logging.getLogger(__name__)

IPC_SOCKET = "/tmp/mpv-pi-decoder.sock"
SCREENSHOT_PATH = "/tmp/mpv-preview.jpg"
IP_OVERLAY_ID = 63
_FAILOVER_THRESHOLD = 3  # consecutive idle checks before switching to backup URL


def _find_drm_device() -> str | None:
    """Find the DRM card that has HDMI connectors (Pi 5 uses card1, Pi 4 uses card0)."""
    import glob as _glob
    connectors = sorted(_glob.glob("/sys/class/drm/card*-HDMI-*"))
    if connectors:
        card = os.path.basename(connectors[0]).split("-")[0]
        return f"/dev/dri/{card}"
    return None


def _get_version() -> str:
    try:
        return pkg_version("pi-decoder")
    except Exception:
        return "dev"


def _get_network_info() -> dict:
    """Get network info for idle screen. Uses nmcli with socket fallback."""
    try:
        from pi_decoder.network import get_network_info_sync
        return get_network_info_sync()
    except Exception:
        # Fallback to socket trick
        info = {"connection_type": "unknown", "ip": "", "ssid": "", "hotspot_active": False, "signal": 0}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                info["ip"] = s.getsockname()[0]
        except Exception:
            pass
        return info


class MpvManager:
    """Manage an mpv child process and communicate via its JSON IPC socket."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._restart_backoff = 3.0
        self._stopping = False
        self._user_stopped = False  # True = user explicitly stopped, skip auto-retry
        self._stream_retry_backoff = 5.0  # Start at 5 seconds
        self._last_stream_attempt = 0.0
        self._last_connection_type = ""  # Track for auto-retry on network change
        self._stderr_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._ipc_lock = asyncio.Lock()
        # Failover: switch to backup URL after consecutive stream failures
        self._stream_failures: int = 0
        self._using_backup: bool = False

    def _ytdl_format(self) -> str:
        """Build the ytdl-format string based on max_resolution config."""
        res = self._config.stream.max_resolution
        if res == "best":
            return "bestvideo+bestaudio/best"
        return f"bestvideo[height<={res}]+bestaudio/best[height<={res}]"

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the mpv subprocess and connect IPC."""
        async with self._lifecycle_lock:
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        """Inner start — called under _lifecycle_lock."""
        self._stopping = False
        # clean up stale socket
        try:
            os.unlink(IPC_SOCKET)
        except FileNotFoundError:
            pass

        drm_dev = _find_drm_device()

        # Point mpv's ytdl_hook at the venv yt-dlp so it uses the
        # up-to-date version instead of the old apt system binary.
        venv_ytdl = "/opt/pi-decoder/venv/bin/yt-dlp"
        ytdl_path_opt = (
            [f"--script-opts=ytdl_hook-ytdl_path={venv_ytdl}"]
            if Path(venv_ytdl).exists()
            else []
        )

        cmd = [
            "mpv",
            "--vo=drm",
            *(["--drm-device=" + drm_dev] if drm_dev else []),
            "--no-terminal",
            f"--hwdec={self._config.stream.hwdec}",
            "--keepaspect=yes",
            f"--input-ipc-server={IPC_SOCKET}",
            "--idle=yes",
            "--cache=yes",
            "--demuxer-max-bytes=50M",
            f"--demuxer-readahead-secs={self._config.stream.network_caching // 1000}",
            "--no-osc",
            "--no-osd-bar",
            "--osd-level=0",
            "--audio-device=auto",
            f"--ytdl-format={self._ytdl_format()}",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
            "--background=0/0/0",  # Pure black when idle
            "--osd-msg1=",  # No OSD messages
            *ytdl_path_opt,
        ]

        if self._config.stream.url:
            cmd.append(self._config.stream.url)

        log.info("Starting mpv: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Drain stderr in background so the pipe buffer never fills (mpv would block)
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # wait for IPC socket to appear
        for _ in range(50):  # up to 5 s
            if Path(IPC_SOCKET).exists():
                break
            await asyncio.sleep(0.1)
        else:
            log.warning("mpv IPC socket did not appear within 5 s")

        await self._connect_ipc()
        self._restart_backoff = 3.0  # reset on successful start

        # start background monitor
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        """Graceful shutdown: quit via IPC, then kill."""
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        """Inner stop — called under _lifecycle_lock."""
        self._stopping = True

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # try IPC quit
        try:
            await self._send(["quit"], timeout=2.0)
        except Exception:
            pass

        await self._disconnect_ipc()

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        self._process = None
        log.info("mpv stopped")

    async def restart(self) -> None:
        """Stop + start under a single lifecycle lock. Propagates start errors."""
        async with self._lifecycle_lock:
            await self._stop_unlocked()
            await asyncio.sleep(0.5)
            await self._start_unlocked()

    # ── commands ─────────────────────────────────────────────────────────

    async def stop_stream(self) -> None:
        """Unload the current stream but keep mpv alive (shows idle screen)."""
        self._user_stopped = True
        await self._send(["stop"])

    async def load_stream(self, url: str) -> None:
        self._user_stopped = False
        await self._send(["loadfile", url])

    async def get_status(self) -> dict:
        """Return a status dict for the web API."""
        result: dict = {"alive": self.is_alive_sync()}
        try:
            pause = await self._get_property("pause")
            idle = await self._get_property("idle-active")
            path = await self._get_property("path")
            result["paused"] = pause
            result["idle"] = idle
            result["playing"] = not pause and not idle
            result["stream_url"] = path or ""
        except Exception:
            result["playing"] = False
            result["idle"] = True
            result["stream_url"] = ""
        # hwdec-current: what decoder mpv actually resolved to
        try:
            hwdec_cur = await self._get_property("hwdec-current")
            result["hwdec_current"] = hwdec_cur or ""
        except Exception:
            result["hwdec_current"] = ""
        # Video performance stats (only meaningful while playing)
        try:
            result["fps"] = await self._get_property("estimated-vf-fps") or 0
            result["dropped_frames"] = await self._get_property("frame-drop-count") or 0
            result["decoder_drops"] = await self._get_property("decoder-frame-drop-count") or 0
            w = await self._get_property("video-params/w")
            h = await self._get_property("video-params/h")
            result["resolution"] = f"{w}x{h}" if w and h else ""
        except Exception:
            result["fps"] = 0
            result["dropped_frames"] = 0
            result["decoder_drops"] = 0
            result["resolution"] = ""
        result["using_backup"] = self._using_backup
        return result

    async def set_overlay(self, overlay_id: int, ass_text: str) -> None:
        """Push an ASS overlay using osd-overlay with explicit resolution."""
        await self._send(
            ["osd-overlay"],
            id=overlay_id, format="ass-events", data=ass_text,
            res_x=1920, res_y=1080,
        )

    async def remove_overlay(self, overlay_id: int) -> None:
        await self._send(["osd-overlay"], id=overlay_id, format="none", data="")

    async def take_screenshot(self) -> bytes | None:
        """Capture a screenshot, return JPEG bytes."""
        try:
            await self._send(["screenshot-to-file", SCREENSHOT_PATH, "video"])
            # give mpv a moment to write the file
            await asyncio.sleep(0.3)
            p = Path(SCREENSHOT_PATH)
            if p.exists():
                data = p.read_bytes()
                p.unlink(missing_ok=True)
                return data
        except Exception:
            log.warning("Screenshot failed", exc_info=True)
        return None

    def is_alive_sync(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def is_alive(self) -> bool:
        if not self.is_alive_sync():
            return False
        # ping via IPC
        try:
            await self._get_property("mpv-version")
            return True
        except Exception:
            return False

    @property
    def using_backup(self) -> bool:
        return self._using_backup

    def reset_stream_retry(self) -> None:
        """Reset retry backoff when stream URL is changed."""
        self._stream_retry_backoff = 5.0
        self._last_stream_attempt = 0.0
        self._user_stopped = False
        self._stream_failures = 0
        self._using_backup = False

    def _build_idle_overlay(self, net: dict) -> str:
        """Build multi-line ASS overlay for idle screen."""
        name = self._config.general.name
        ver = _get_version()
        ip = net.get("ip", "")
        conn_type = net.get("connection_type", "none")
        ssid = net.get("ssid", "")
        signal = net.get("signal", 0)
        hotspot_active = net.get("hotspot_active", False)

        # Title style
        head = r"{\an7\fs22\b1\1c&HFFFFFF&\3c&H000000&\bord2}"
        body = r"{\fs16\b0}"
        sep = r"{\fs14}" + "\u2500" * 28
        accent = r"{\fs16\1c&H00BFFF&}"  # Orange in BGR for hotspot

        lines = []
        lines.append(f"{head}{name} v{ver}")
        lines.append(sep)

        # Network line
        if conn_type == "ethernet":
            lines.append(f"{body}Network: Ethernet")
        elif conn_type == "wifi":
            sig_str = f" {signal}%" if signal else ""
            lines.append(f"{body}Network: WiFi ({ssid}){sig_str}")
        elif conn_type == "hotspot":
            lines.append(f"{accent}Network: Hotspot")
        else:
            lines.append(f"{body}Network: Not connected")

        # IP line
        if ip:
            lines.append(f"{body}IP: {ip}")
            lines.append(f"{body}Web UI: http://{ip}")
            lines.append(f"{body}        http://{socket.gethostname()}.local")
        else:
            lines.append(f"{body}IP: No network")

        # Stream status
        if not self._config.stream.url:
            lines.append(f"{body}Stream: No URL configured")
        else:
            now = time.monotonic()
            time_since = now - self._last_stream_attempt if self._last_stream_attempt else 0
            time_until = max(0, self._stream_retry_backoff - time_since)
            if time_until > 0 and self._last_stream_attempt > 0:
                lines.append(f"{body}Stream: Retrying in {int(time_until)}s...")
            else:
                lines.append(f"{body}Stream: Connecting...")

        # Hotspot credentials
        if hotspot_active:
            lines.append("")
            lines.append(f"{accent}WiFi Setup:")
            hs_ssid = self._config.network.hotspot_ssid
            hs_pass = self._config.network.hotspot_password
            lines.append(f"{accent}  Network: {hs_ssid}")
            lines.append(f"{accent}  Password: {hs_pass}")

        return "\\N".join(lines)

    async def _read_stderr(self) -> None:
        """Continuously drain mpv stderr and log warnings."""
        try:
            while self._process and self._process.stderr:
                line = await self._process.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="replace").rstrip()
                if msg:
                    log.warning("mpv: %s", msg)
        except Exception:
            pass

    # ── IPC internals ────────────────────────────────────────────────────

    async def _connect_ipc(self) -> None:
        for attempt in range(5):
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    IPC_SOCKET
                )
                self._read_task = asyncio.create_task(self._ipc_reader())
                log.info("Connected to mpv IPC socket")
                return
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                await asyncio.sleep(0.5)
        log.warning("Could not connect to mpv IPC after retries")

    async def _disconnect_ipc(self) -> None:
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        # cancel all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def _ipc_reader(self) -> None:
        """Read lines from IPC socket and resolve pending futures."""
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = msg.get("request_id")
                if rid and rid in self._pending:
                    fut = self._pending.pop(rid)
                    if not fut.done():
                        if msg.get("error") == "success":
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(
                                RuntimeError(msg.get("error", "unknown IPC error"))
                            )
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("IPC reader ended", exc_info=True)

    async def _send(self, command: list, timeout: float = 5.0, **named_args):
        """Send a JSON command and wait for the response.

        Extra keyword arguments are merged into the top-level message as
        named parameters (used by commands like ``osd-overlay``).
        Uses an IPC lock to ensure atomic send/receive pairs.
        """
        async with self._ipc_lock:
            if not self._writer:
                raise RuntimeError("IPC not connected")
            self._request_id += 1
            rid = self._request_id
            msg = {"command": command, "request_id": rid, **named_args}
            payload = json.dumps(msg) + "\n"
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            self._writer.write(payload.encode())
            await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise

    async def _get_property(self, name: str):
        return await self._send(["get_property", name])

    # ── health monitor ───────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        """Monitor mpv subprocess health, auto-restart on failure."""
        try:
            while not self._stopping:
                await asyncio.sleep(5)
                if self._stopping:
                    break
                if not self.is_alive_sync():
                    log.warning("mpv process died — restarting in %.0fs", self._restart_backoff)
                    await asyncio.sleep(self._restart_backoff)
                    if self._stopping:
                        break
                    self._restart_backoff = min(self._restart_backoff * 2, 60.0)
                    await self._disconnect_ipc()
                    await self.start()
                    continue
                # IPC ping
                try:
                    await asyncio.wait_for(
                        self._get_property("mpv-version"), timeout=10.0
                    )
                except Exception:
                    log.warning("mpv IPC unresponsive — killing and restarting")
                    if self._process and self._process.returncode is None:
                        self._process.kill()
                        await self._process.wait()
                    await self._disconnect_ipc()
                    await asyncio.sleep(self._restart_backoff)
                    if self._stopping:
                        break
                    self._restart_backoff = min(self._restart_backoff * 2, 60.0)
                    await self.start()
                    continue

                # Stream health check: auto-retry if idle but we have a URL configured
                try:
                    status = await self.get_status()
                    if status.get("idle"):
                        # Build enhanced idle overlay
                        try:
                            net = _get_network_info()
                            ass = self._build_idle_overlay(net)
                            await self.set_overlay(IP_OVERLAY_ID, ass)

                            # Auto-retry on network change
                            conn_type = net.get("connection_type", "")
                            if (self._last_connection_type
                                    and conn_type != self._last_connection_type
                                    and conn_type not in ("none", "hotspot")):
                                log.info("Network changed (%s -> %s), resetting stream retry",
                                         self._last_connection_type, conn_type)
                                self.reset_stream_retry()
                            self._last_connection_type = conn_type
                        except Exception:
                            log.warning("Idle overlay push failed", exc_info=True)

                        if self._config.stream.url and not self._user_stopped:
                            # Stream not playing but we have a URL configured
                            now = time.monotonic()
                            if now - self._last_stream_attempt > self._stream_retry_backoff:
                                self._stream_failures += 1
                                # Failover: after N consecutive failures, try backup URL
                                use_url = self._config.stream.url
                                backup = self._config.stream.backup_url
                                if (backup and self._stream_failures >= _FAILOVER_THRESHOLD
                                        and not self._using_backup):
                                    log.warning(
                                        "Stream failed %d times, switching to backup: %s",
                                        self._stream_failures, backup,
                                    )
                                    use_url = backup
                                    self._using_backup = True
                                elif self._using_backup and backup:
                                    use_url = backup
                                log.info("Stream idle, attempting to reload: %s", use_url)
                                self._last_stream_attempt = now
                                try:
                                    await self.load_stream(use_url)
                                    # Increase backoff for next attempt (max 60s)
                                    self._stream_retry_backoff = min(self._stream_retry_backoff * 1.5, 60.0)
                                except Exception:
                                    log.debug("Stream reload failed, will retry", exc_info=True)
                    else:
                        # Stream is playing, remove IP overlay and reset backoff
                        try:
                            await self.remove_overlay(IP_OVERLAY_ID)
                        except Exception:
                            pass
                        self._stream_retry_backoff = 5.0
                        self._stream_failures = 0
                        # Track connection type for change detection
                        try:
                            net = _get_network_info()
                            self._last_connection_type = net.get("connection_type", "")
                        except Exception:
                            pass
                except Exception:
                    log.debug("Stream health check failed", exc_info=True)
        except asyncio.CancelledError:
            return
