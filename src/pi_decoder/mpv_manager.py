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

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the mpv subprocess and connect IPC."""
        self._stopping = False
        # clean up stale socket
        try:
            os.unlink(IPC_SOCKET)
        except FileNotFoundError:
            pass

        cmd = [
            "mpv",
            "--fullscreen",
            "--no-terminal",
            "--hwdec=auto",
            f"--input-ipc-server={IPC_SOCKET}",
            "--idle=yes",
            "--force-window=yes",
            "--cache=yes",
            "--demuxer-max-bytes=5M",
            "--demuxer-readahead-secs=5",
            "--no-osc",
            "--no-osd-bar",
            "--osd-level=0",
            "--cursor-autohide=always",
            "--audio-device=auto",
            "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
            "--background=color",
            "--background-color=0/0/0",  # Pure black when idle
            "--osd-msg1=",  # No OSD messages
        ]

        if self._config.stream.url:
            cmd.append(self._config.stream.url)

        log.info("Starting mpv: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

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

        self._process = None
        log.info("mpv stopped")

    async def restart(self) -> None:
        """Stop + start."""
        await self.stop()
        await asyncio.sleep(0.5)
        await self.start()

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
        return result

    async def set_overlay(self, overlay_id: int, ass_text: str) -> None:
        """Push an ASS overlay using osd-overlay command."""
        await self._send([
            "osd-overlay",
            overlay_id,
            "ass-events",
            ass_text,
        ])

    async def remove_overlay(self, overlay_id: int) -> None:
        await self._send(["osd-overlay", overlay_id, "none", ""])

    async def take_screenshot(self) -> bytes | None:
        """Capture a screenshot, return JPEG bytes."""
        try:
            await self._send(["screenshot-to-file", SCREENSHOT_PATH, "window"])
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

    def reset_stream_retry(self) -> None:
        """Reset retry backoff when stream URL is changed."""
        self._stream_retry_backoff = 5.0
        self._last_stream_attempt = 0.0
        self._user_stopped = False

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

    async def _send(self, command: list, timeout: float = 5.0):
        """Send a JSON command and wait for the response."""
        if not self._writer:
            raise RuntimeError("IPC not connected")
        self._request_id += 1
        rid = self._request_id
        msg = {"command": command, "request_id": rid}
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
                                log.info("Stream idle, attempting to reload: %s", self._config.stream.url)
                                self._last_stream_attempt = now
                                try:
                                    await self.load_stream(self._config.stream.url)
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
