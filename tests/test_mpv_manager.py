"""Tests for MpvManager — mpv process lifecycle and JSON IPC client."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_decoder.config import Config
from pi_decoder.mpv_manager import MpvManager, IPC_SOCKET, SCREENSHOT_PATH, IP_OVERLAY_ID


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_config(**overrides) -> Config:
    """Create a Config with sensible defaults, applying any overrides."""
    cfg = Config()
    for key, val in overrides.items():
        section, attr = key.split(".", 1)
        setattr(getattr(cfg, section), attr, val)
    return cfg


def _make_manager(config: Config | None = None) -> MpvManager:
    """Create an MpvManager with a default config."""
    return MpvManager(config or _make_config())


def _attach_mock_writer(mgr: MpvManager) -> MagicMock:
    """Attach a mock writer to the manager and return it."""
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    mgr._writer = writer
    return writer


def _attach_mock_process(mgr: MpvManager, alive: bool = True) -> MagicMock:
    """Attach a mock subprocess to the manager and return it."""
    proc = MagicMock()
    proc.returncode = None if alive else 1
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    mgr._process = proc
    return proc


# ── Constants ────────────────────────────────────────────────────────────────


class TestConstants:
    def test_ipc_socket_path(self):
        assert IPC_SOCKET == "/tmp/mpv-pi-decoder.sock"

    def test_screenshot_path(self):
        assert SCREENSHOT_PATH == "/tmp/mpv-preview.jpg"

    def test_ip_overlay_id(self):
        assert IP_OVERLAY_ID == 63


# ── Init ─────────────────────────────────────────────────────────────────────


class TestInit:
    def test_initial_state(self):
        cfg = _make_config()
        mgr = MpvManager(cfg)
        assert mgr._config is cfg
        assert mgr._process is None
        assert mgr._reader is None
        assert mgr._writer is None
        assert mgr._request_id == 0
        assert mgr._pending == {}
        assert mgr._read_task is None
        assert mgr._monitor_task is None
        assert mgr._restart_backoff == 3.0
        assert mgr._stopping is False
        assert mgr._user_stopped is False
        assert mgr._stream_retry_backoff == 5.0
        assert mgr._last_stream_attempt == 0.0
        assert mgr._last_connection_type == ""


# ── _send ────────────────────────────────────────────────────────────────────


class TestSend:
    async def test_send_raises_when_no_writer(self):
        mgr = _make_manager()
        with pytest.raises(RuntimeError, match="IPC not connected"):
            await mgr._send(["stop"])

    async def test_send_writes_json_and_waits(self):
        mgr = _make_manager()
        writer = _attach_mock_writer(mgr)

        async def _resolve():
            """Simulate IPC reader resolving the future after a tiny delay."""
            await asyncio.sleep(0.01)
            rid = mgr._request_id
            fut = mgr._pending.get(rid)
            if fut and not fut.done():
                fut.set_result("ok")

        task = asyncio.create_task(_resolve())
        result = await mgr._send(["stop"], timeout=2.0)
        await task

        assert result == "ok"
        writer.write.assert_called_once()
        payload = writer.write.call_args[0][0]
        msg = json.loads(payload.decode())
        assert msg["command"] == ["stop"]
        assert "request_id" in msg
        writer.drain.assert_awaited_once()

    async def test_send_increments_request_id(self):
        mgr = _make_manager()
        _attach_mock_writer(mgr)

        for i in range(1, 4):
            async def _resolve(expected_id=i):
                await asyncio.sleep(0.01)
                fut = mgr._pending.get(expected_id)
                if fut and not fut.done():
                    fut.set_result(None)

            task = asyncio.create_task(_resolve())
            await mgr._send(["noop"], timeout=2.0)
            await task
            assert mgr._request_id == i

    async def test_send_timeout_cleans_up_pending(self):
        mgr = _make_manager()
        _attach_mock_writer(mgr)

        with pytest.raises(asyncio.TimeoutError):
            await mgr._send(["hang"], timeout=0.05)

        # pending dict should be cleaned up after timeout
        assert len(mgr._pending) == 0

    async def test_send_propagates_ipc_error(self):
        mgr = _make_manager()
        _attach_mock_writer(mgr)

        async def _resolve_error():
            await asyncio.sleep(0.01)
            rid = mgr._request_id
            fut = mgr._pending.get(rid)
            if fut and not fut.done():
                fut.set_exception(RuntimeError("property not found"))

        task = asyncio.create_task(_resolve_error())
        with pytest.raises(RuntimeError, match="property not found"):
            await mgr._send(["get_property", "nonexistent"], timeout=2.0)
        await task


# ── _get_property ────────────────────────────────────────────────────────────


class TestGetProperty:
    async def test_get_property_delegates_to_send(self):
        mgr = _make_manager()
        mgr._send = AsyncMock(return_value="mpv 0.38.0")
        result = await mgr._get_property("mpv-version")
        mgr._send.assert_awaited_once_with(["get_property", "mpv-version"])
        assert result == "mpv 0.38.0"


# ── is_alive_sync / is_alive ────────────────────────────────────────────────


class TestIsAlive:
    def test_is_alive_sync_no_process(self):
        mgr = _make_manager()
        assert mgr.is_alive_sync() is False

    def test_is_alive_sync_alive_process(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)
        assert mgr.is_alive_sync() is True

    def test_is_alive_sync_dead_process(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=False)
        assert mgr.is_alive_sync() is False

    async def test_is_alive_async_no_process(self):
        mgr = _make_manager()
        assert await mgr.is_alive() is False

    async def test_is_alive_async_alive_with_ipc(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)
        mgr._get_property = AsyncMock(return_value="mpv 0.38.0")
        assert await mgr.is_alive() is True
        mgr._get_property.assert_awaited_once_with("mpv-version")

    async def test_is_alive_async_alive_no_ipc(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)
        mgr._get_property = AsyncMock(side_effect=RuntimeError("IPC not connected"))
        assert await mgr.is_alive() is False


# ── Stream commands ──────────────────────────────────────────────────────────


class TestStreamCommands:
    async def test_stop_stream(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        await mgr.stop_stream()
        mgr._send.assert_awaited_once_with(["stop"])

    async def test_load_stream(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        url = "http://example.com/stream.m3u8"
        await mgr.load_stream(url)
        mgr._send.assert_awaited_once_with(["loadfile", url])

    async def test_stop_stream_sets_user_stopped(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        assert mgr._user_stopped is False
        await mgr.stop_stream()
        assert mgr._user_stopped is True

    async def test_load_stream_clears_user_stopped(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._user_stopped = True
        await mgr.load_stream("http://example.com/stream.m3u8")
        assert mgr._user_stopped is False


# ── Overlay commands ─────────────────────────────────────────────────────────


class TestOverlayCommands:
    async def test_set_overlay(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        await mgr.set_overlay(42, r"{\an7\fs22}Hello")
        mgr._send.assert_awaited_once_with(
            ["osd-overlay"],
            id=42, format="ass-events", data=r"{\an7\fs22}Hello",
            res_x=1920, res_y=1080,
        )

    async def test_remove_overlay(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        await mgr.remove_overlay(63)
        mgr._send.assert_awaited_once_with(
            ["osd-overlay"], id=63, format="none", data="",
        )


# ── get_status ───────────────────────────────────────────────────────────────


class TestGetStatus:
    async def test_status_when_playing(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)
        mgr._get_property = AsyncMock(side_effect=[
            False,   # pause
            False,   # idle-active
            "http://example.com/stream.m3u8",  # path
            "v4l2m2m",  # hwdec-current
        ])
        status = await mgr.get_status()
        assert status["alive"] is True
        assert status["paused"] is False
        assert status["idle"] is False
        assert status["playing"] is True
        assert status["stream_url"] == "http://example.com/stream.m3u8"
        assert status["hwdec_current"] == "v4l2m2m"

    async def test_status_when_paused(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)
        mgr._get_property = AsyncMock(side_effect=[
            True,    # pause
            False,   # idle-active
            "http://example.com/stream.m3u8",  # path
            "",      # hwdec-current
        ])
        status = await mgr.get_status()
        assert status["paused"] is True
        assert status["playing"] is False

    async def test_status_when_idle(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)
        mgr._get_property = AsyncMock(side_effect=[
            False,   # pause
            True,    # idle-active
            None,    # path
            None,    # hwdec-current
        ])
        status = await mgr.get_status()
        assert status["idle"] is True
        assert status["playing"] is False
        assert status["stream_url"] == ""
        assert status["hwdec_current"] == ""

    async def test_status_on_ipc_error(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)
        mgr._get_property = AsyncMock(side_effect=RuntimeError("IPC dead"))
        status = await mgr.get_status()
        assert status["alive"] is True
        assert status["playing"] is False
        assert status["idle"] is True
        assert status["stream_url"] == ""
        assert status["hwdec_current"] == ""

    async def test_status_no_process(self):
        mgr = _make_manager()
        mgr._get_property = AsyncMock(side_effect=RuntimeError("nope"))
        status = await mgr.get_status()
        assert status["alive"] is False


# ── take_screenshot ──────────────────────────────────────────────────────────


class TestScreenshot:
    async def test_take_screenshot_success(self, tmp_path):
        mgr = _make_manager()
        mgr._send = AsyncMock()

        fake_jpg = b"\xff\xd8\xff\xe0JFIF-fake-screenshot"
        fake_path = tmp_path / "mpv-preview.jpg"
        fake_path.write_bytes(fake_jpg)

        with patch("pi_decoder.mpv_manager.SCREENSHOT_PATH", str(fake_path)), \
             patch("pi_decoder.mpv_manager.Path") as MockPath, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            # Make Path(SCREENSHOT_PATH) return a mock whose exists/read_bytes work
            path_inst = MagicMock()
            path_inst.exists.return_value = True
            path_inst.read_bytes.return_value = fake_jpg
            path_inst.unlink = MagicMock()
            MockPath.return_value = path_inst

            data = await mgr.take_screenshot()

        assert data == fake_jpg
        mgr._send.assert_awaited_once_with(["screenshot-to-file", str(fake_path), "video"])
        path_inst.unlink.assert_called_once_with(missing_ok=True)

    async def test_take_screenshot_no_file(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()

        with patch("pi_decoder.mpv_manager.Path") as MockPath, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            path_inst = MagicMock()
            path_inst.exists.return_value = False
            MockPath.return_value = path_inst

            data = await mgr.take_screenshot()

        assert data is None

    async def test_take_screenshot_send_failure(self):
        mgr = _make_manager()
        mgr._send = AsyncMock(side_effect=RuntimeError("IPC not connected"))

        data = await mgr.take_screenshot()
        assert data is None


# ── reset_stream_retry ───────────────────────────────────────────────────────


class TestResetStreamRetry:
    def test_reset_stream_retry(self):
        mgr = _make_manager()
        mgr._stream_retry_backoff = 30.0
        mgr._last_stream_attempt = 99999.9
        mgr.reset_stream_retry()
        assert mgr._stream_retry_backoff == 5.0
        assert mgr._last_stream_attempt == 0.0

    def test_reset_stream_retry_clears_user_stopped(self):
        mgr = _make_manager()
        mgr._user_stopped = True
        mgr.reset_stream_retry()
        assert mgr._user_stopped is False


# ── _build_idle_overlay ──────────────────────────────────────────────────────


class TestBuildIdleOverlay:
    def _make_net(self, **kwargs):
        base = {
            "connection_type": "none",
            "ip": "",
            "ssid": "",
            "hotspot_active": False,
            "signal": 0,
        }
        base.update(kwargs)
        return base

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.2.3")
    def test_ethernet_connected(self, _mock_ver):
        cfg = _make_config(**{"general.name": "MyDecoder"})
        mgr = MpvManager(cfg)
        net = self._make_net(connection_type="ethernet", ip="192.168.1.100")
        result = mgr._build_idle_overlay(net)

        assert "MyDecoder v1.2.3" in result
        assert "Network: Ethernet" in result
        assert "IP: 192.168.1.100" in result
        assert "http://192.168.1.100" in result
        # ASS newline separator
        assert "\\N" in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_wifi_connected(self, _mock_ver):
        mgr = _make_manager()
        net = self._make_net(connection_type="wifi", ip="10.0.0.5", ssid="MyNet", signal=75)
        result = mgr._build_idle_overlay(net)

        assert "Network: WiFi (MyNet) 75%" in result
        assert "IP: 10.0.0.5" in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_wifi_no_signal(self, _mock_ver):
        mgr = _make_manager()
        net = self._make_net(connection_type="wifi", ip="10.0.0.5", ssid="MyNet", signal=0)
        result = mgr._build_idle_overlay(net)

        # signal=0 should omit signal percentage
        assert "Network: WiFi (MyNet)" in result
        assert "75%" not in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_hotspot_active(self, _mock_ver):
        cfg = _make_config(**{
            "network.hotspot_ssid": "PiDec-Setup",
            "network.hotspot_password": "secret123",
        })
        mgr = MpvManager(cfg)
        net = self._make_net(connection_type="hotspot", ip="10.42.0.1", hotspot_active=True)
        result = mgr._build_idle_overlay(net)

        assert "Network: Hotspot" in result
        assert "WiFi Setup:" in result
        assert "Network: PiDec-Setup" in result
        assert "Password: secret123" in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_no_connection(self, _mock_ver):
        mgr = _make_manager()
        net = self._make_net(connection_type="none", ip="")
        result = mgr._build_idle_overlay(net)

        assert "Network: Not connected" in result
        assert "IP: No network" in result
        # Should NOT contain "Web UI" when no IP
        assert "Web UI" not in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_unknown_connection(self, _mock_ver):
        mgr = _make_manager()
        net = self._make_net(connection_type="unknown", ip="")
        result = mgr._build_idle_overlay(net)

        assert "Network: Not connected" in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_stream_url_configured(self, _mock_ver):
        cfg = _make_config(**{"stream.url": "http://example.com/stream"})
        mgr = MpvManager(cfg)
        mgr._last_stream_attempt = 0.0
        net = self._make_net(connection_type="ethernet", ip="192.168.1.5")
        result = mgr._build_idle_overlay(net)

        assert "Stream: Connecting..." in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_stream_retry_countdown(self, _mock_ver):
        cfg = _make_config(**{"stream.url": "http://example.com/stream"})
        mgr = MpvManager(cfg)
        mgr._stream_retry_backoff = 30.0
        mgr._last_stream_attempt = time.monotonic()  # just attempted
        net = self._make_net(connection_type="ethernet", ip="192.168.1.5")
        result = mgr._build_idle_overlay(net)

        assert "Stream: Retrying in" in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_no_stream_url_configured(self, _mock_ver):
        cfg = _make_config(**{"stream.url": ""})
        mgr = MpvManager(cfg)
        net = self._make_net(connection_type="ethernet", ip="192.168.1.5")
        result = mgr._build_idle_overlay(net)

        assert "Stream: No URL configured" in result

    @patch("pi_decoder.mpv_manager._get_version", return_value="1.0.0")
    def test_hotspot_not_active_no_credentials(self, _mock_ver):
        """When hotspot_active=False, credentials should not appear."""
        mgr = _make_manager()
        net = self._make_net(connection_type="ethernet", ip="10.0.0.1", hotspot_active=False)
        result = mgr._build_idle_overlay(net)

        assert "WiFi Setup:" not in result
        assert "Password:" not in result


# ── Process lifecycle: start ─────────────────────────────────────────────────


class TestStart:
    @patch("pi_decoder.mpv_manager.Path")
    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_start_launches_mpv_and_connects(
        self, mock_sleep, mock_exec, mock_path_cls
    ):
        mgr = _make_manager()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        mgr._connect_ipc = AsyncMock()

        # Socket appears immediately
        mock_path_cls.return_value.exists.return_value = True

        # Prevent the health loop from actually running
        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock(done=MagicMock(return_value=False))
            await mgr.start()

        mock_exec.assert_awaited_once()
        # First arg should be 'mpv'
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "mpv"
        # IPC socket arg should be present
        assert any(IPC_SOCKET in str(a) for a in call_args)

        mgr._connect_ipc.assert_awaited_once()
        assert mgr._process is mock_proc
        assert mgr._restart_backoff == 3.0
        assert mgr._stopping is False

    @patch("pi_decoder.mpv_manager.Path")
    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_start_includes_stream_url_when_configured(
        self, mock_sleep, mock_exec, mock_path_cls
    ):
        cfg = _make_config(**{"stream.url": "http://example.com/live.m3u8"})
        mgr = MpvManager(cfg)
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        mgr._connect_ipc = AsyncMock()
        mock_path_cls.return_value.exists.return_value = True

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock(done=MagicMock(return_value=False))
            await mgr.start()

        call_args = mock_exec.call_args[0]
        assert "http://example.com/live.m3u8" in call_args

    @patch("pi_decoder.mpv_manager.Path")
    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_start_excludes_stream_url_when_empty(
        self, mock_sleep, mock_exec, mock_path_cls
    ):
        cfg = _make_config(**{"stream.url": ""})
        mgr = MpvManager(cfg)
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        mgr._connect_ipc = AsyncMock()
        mock_path_cls.return_value.exists.return_value = True

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock(done=MagicMock(return_value=False))
            await mgr.start()

        call_args = mock_exec.call_args[0]
        # No empty string or stream URL should appear
        assert "" not in call_args[1:]  # skip 'mpv' itself

    @patch("pi_decoder.mpv_manager.Path")
    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_start_uses_hwdec_from_config(
        self, mock_sleep, mock_exec, mock_path_cls
    ):
        cfg = _make_config(**{"stream.hwdec": "v4l2m2m"})
        mgr = MpvManager(cfg)
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        mgr._connect_ipc = AsyncMock()
        mock_path_cls.return_value.exists.return_value = True

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock(done=MagicMock(return_value=False))
            await mgr.start()

        call_args = mock_exec.call_args[0]
        assert "--hwdec=v4l2m2m" in call_args

    @patch("pi_decoder.mpv_manager.Path")
    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_start_defaults_to_hwdec_auto(
        self, mock_sleep, mock_exec, mock_path_cls
    ):
        mgr = _make_manager()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        mgr._connect_ipc = AsyncMock()
        mock_path_cls.return_value.exists.return_value = True

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock(done=MagicMock(return_value=False))
            await mgr.start()

        call_args = mock_exec.call_args[0]
        assert "--hwdec=auto" in call_args

    @patch("os.unlink")
    @patch("pi_decoder.mpv_manager.Path")
    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_start_removes_stale_socket(
        self, mock_sleep, mock_exec, mock_path_cls, mock_unlink
    ):
        mgr = _make_manager()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        mgr._connect_ipc = AsyncMock()
        mock_path_cls.return_value.exists.return_value = True

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock(done=MagicMock(return_value=False))
            await mgr.start()

        mock_unlink.assert_called_once_with(IPC_SOCKET)

    @patch("os.unlink", side_effect=FileNotFoundError)
    @patch("pi_decoder.mpv_manager.Path")
    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_start_tolerates_missing_stale_socket(
        self, mock_sleep, mock_exec, mock_path_cls, mock_unlink
    ):
        mgr = _make_manager()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        mgr._connect_ipc = AsyncMock()
        mock_path_cls.return_value.exists.return_value = True

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock(done=MagicMock(return_value=False))
            await mgr.start()

        # Should not raise -- FileNotFoundError is handled
        assert mgr._process is mock_proc


# ── Process lifecycle: stop ──────────────────────────────────────────────────


class TestStop:
    async def test_stop_sets_stopping_flag(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._disconnect_ipc = AsyncMock()
        await mgr.stop()
        assert mgr._stopping is True

    async def test_stop_cancels_monitor_task(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._disconnect_ipc = AsyncMock()

        # Use a real asyncio task so done()/cancel()/await all work correctly
        async def _wait_forever():
            await asyncio.Event().wait()

        monitor = asyncio.create_task(_wait_forever())
        mgr._monitor_task = monitor

        await mgr.stop()

        assert monitor.cancelled()

    async def test_stop_sends_ipc_quit(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._disconnect_ipc = AsyncMock()
        await mgr.stop()
        mgr._send.assert_awaited_once_with(["quit"], timeout=2.0)

    async def test_stop_tolerates_ipc_quit_failure(self):
        mgr = _make_manager()
        mgr._send = AsyncMock(side_effect=RuntimeError("IPC dead"))
        mgr._disconnect_ipc = AsyncMock()
        # Should not raise
        await mgr.stop()

    async def test_stop_terminates_running_process(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._disconnect_ipc = AsyncMock()

        proc = _attach_mock_process(mgr, alive=True)
        proc.wait = AsyncMock()

        await mgr.stop()

        proc.terminate.assert_called_once()
        assert mgr._process is None

    async def test_stop_kills_process_on_terminate_timeout(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._disconnect_ipc = AsyncMock()

        proc = _attach_mock_process(mgr, alive=True)
        # After kill(), the bare `await proc.wait()` should succeed
        proc.wait = AsyncMock(return_value=None)

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            await mgr.stop()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert mgr._process is None

    async def test_stop_skips_terminate_when_already_dead(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._disconnect_ipc = AsyncMock()
        proc = _attach_mock_process(mgr, alive=False)

        await mgr.stop()

        # Process already exited, terminate/kill should NOT be called
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        assert mgr._process is None

    async def test_stop_disconnects_ipc(self):
        mgr = _make_manager()
        mgr._send = AsyncMock()
        mgr._disconnect_ipc = AsyncMock()
        await mgr.stop()
        mgr._disconnect_ipc.assert_awaited_once()


# ── Process lifecycle: restart ───────────────────────────────────────────────


class TestRestart:
    async def test_restart_calls_stop_then_start(self):
        mgr = _make_manager()
        call_order = []

        async def mock_stop():
            call_order.append("stop")

        async def mock_start():
            call_order.append("start")

        mgr.stop = mock_stop
        mgr.start = mock_start

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.restart()

        assert call_order == ["stop", "start"]


# ── IPC reader ───────────────────────────────────────────────────────────────


class TestIpcReader:
    async def test_ipc_reader_resolves_pending_future(self):
        mgr = _make_manager()
        loop = asyncio.get_running_loop()

        # Create a mock reader that yields one response then EOF
        response = json.dumps({"request_id": 1, "error": "success", "data": "v0.38"}) + "\n"
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=[
            response.encode(),
            b"",  # EOF
        ])
        mgr._reader = reader

        fut = loop.create_future()
        mgr._pending[1] = fut

        await mgr._ipc_reader()

        assert fut.done()
        assert fut.result() == "v0.38"
        assert 1 not in mgr._pending

    async def test_ipc_reader_sets_exception_on_error(self):
        mgr = _make_manager()
        loop = asyncio.get_running_loop()

        response = json.dumps({"request_id": 2, "error": "property not found"}) + "\n"
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=[
            response.encode(),
            b"",
        ])
        mgr._reader = reader

        fut = loop.create_future()
        mgr._pending[2] = fut

        await mgr._ipc_reader()

        assert fut.done()
        with pytest.raises(RuntimeError, match="property not found"):
            fut.result()

    async def test_ipc_reader_ignores_invalid_json(self):
        mgr = _make_manager()

        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=[
            b"this is not json\n",
            b"",
        ])
        mgr._reader = reader

        # Should not raise
        await mgr._ipc_reader()

    async def test_ipc_reader_ignores_messages_without_request_id(self):
        mgr = _make_manager()

        # mpv sends events without request_id
        event = json.dumps({"event": "playback-restart"}) + "\n"
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=[
            event.encode(),
            b"",
        ])
        mgr._reader = reader

        await mgr._ipc_reader()
        # Should not raise; pending dict should remain empty
        assert mgr._pending == {}

    async def test_ipc_reader_ignores_unknown_request_ids(self):
        mgr = _make_manager()
        response = json.dumps({"request_id": 999, "error": "success", "data": "x"}) + "\n"
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=[
            response.encode(),
            b"",
        ])
        mgr._reader = reader

        # No matching pending future -- should not raise
        await mgr._ipc_reader()


# ── IPC connect / disconnect ─────────────────────────────────────────────────


class TestIpcConnect:
    @patch("asyncio.open_unix_connection", new_callable=AsyncMock)
    async def test_connect_ipc_success(self, mock_open):
        mgr = _make_manager()
        mock_reader = MagicMock()
        mock_writer = MagicMock()
        mock_open.return_value = (mock_reader, mock_writer)

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock()
            await mgr._connect_ipc()

        mock_open.assert_awaited_once_with(IPC_SOCKET)
        assert mgr._reader is mock_reader
        assert mgr._writer is mock_writer

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("asyncio.open_unix_connection", new_callable=AsyncMock)
    async def test_connect_ipc_retries_on_failure(self, mock_open, mock_sleep):
        mgr = _make_manager()
        mock_reader = MagicMock()
        mock_writer = MagicMock()
        mock_open.side_effect = [
            ConnectionRefusedError,
            FileNotFoundError,
            (mock_reader, mock_writer),
        ]

        with patch("asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock()
            await mgr._connect_ipc()

        assert mock_open.await_count == 3
        assert mgr._reader is mock_reader

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("asyncio.open_unix_connection", new_callable=AsyncMock)
    async def test_connect_ipc_gives_up_after_5_retries(self, mock_open, mock_sleep):
        mgr = _make_manager()
        mock_open.side_effect = ConnectionRefusedError

        await mgr._connect_ipc()

        assert mock_open.await_count == 5
        assert mgr._reader is None
        assert mgr._writer is None


class TestIpcDisconnect:
    async def test_disconnect_cancels_read_task(self):
        mgr = _make_manager()

        # Use a real asyncio task so done()/cancel()/await all work correctly
        async def _wait_forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(_wait_forever())
        mgr._read_task = task

        await mgr._disconnect_ipc()

        assert task.cancelled()

    async def test_disconnect_closes_writer(self):
        mgr = _make_manager()
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        mgr._writer = writer

        await mgr._disconnect_ipc()

        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()
        assert mgr._writer is None
        assert mgr._reader is None

    async def test_disconnect_cancels_pending_futures(self):
        mgr = _make_manager()
        loop = asyncio.get_running_loop()

        fut1 = loop.create_future()
        fut2 = loop.create_future()
        mgr._pending = {1: fut1, 2: fut2}

        await mgr._disconnect_ipc()

        assert fut1.cancelled()
        assert fut2.cancelled()
        assert mgr._pending == {}

    async def test_disconnect_tolerates_writer_close_error(self):
        mgr = _make_manager()
        writer = MagicMock()
        writer.close = MagicMock(side_effect=OSError("broken pipe"))
        writer.wait_closed = AsyncMock()
        mgr._writer = writer

        # Should not raise
        await mgr._disconnect_ipc()
        assert mgr._writer is None


# ── Health loop ──────────────────────────────────────────────────────────────


class TestHealthLoop:
    async def test_health_loop_exits_when_stopping(self):
        mgr = _make_manager()
        mgr._stopping = True

        # Should return quickly since _stopping is True from the start.
        # The loop checks _stopping after the first sleep.
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr._health_loop()

    async def test_health_loop_restarts_dead_process(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=False)

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                mgr._stopping = True

        mgr._disconnect_ipc = AsyncMock()
        mgr.start = AsyncMock()

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await mgr._health_loop()

        mgr._disconnect_ipc.assert_awaited()
        mgr.start.assert_awaited()

    async def test_health_loop_restarts_on_ipc_unresponsive(self):
        mgr = _make_manager()
        proc = _attach_mock_process(mgr, alive=True)

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                mgr._stopping = True

        mgr._get_property = AsyncMock(side_effect=asyncio.TimeoutError)
        mgr._disconnect_ipc = AsyncMock()
        mgr.start = AsyncMock()

        with patch("asyncio.sleep", side_effect=fake_sleep), \
             patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            await mgr._health_loop()

        proc.kill.assert_called()
        mgr._disconnect_ipc.assert_awaited()
        mgr.start.assert_awaited()

    async def test_health_loop_doubles_backoff_on_restart(self):
        mgr = _make_manager()
        mgr._restart_backoff = 3.0
        _attach_mock_process(mgr, alive=False)

        restart_count = 0

        async def fake_start():
            nonlocal restart_count
            restart_count += 1
            # Process still dead after restart, so loop detects death again
            if restart_count >= 2:
                mgr._stopping = True

        mgr._disconnect_ipc = AsyncMock()
        mgr.start = fake_start

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr._health_loop()

        # After first restart: min(3.0 * 2, 60.0) = 6.0
        # After second restart: min(6.0 * 2, 60.0) = 12.0
        assert mgr._restart_backoff == 12.0

    async def test_health_loop_caps_backoff_at_60(self):
        mgr = _make_manager()
        mgr._restart_backoff = 50.0
        _attach_mock_process(mgr, alive=False)

        restart_count = 0

        async def fake_start():
            nonlocal restart_count
            restart_count += 1
            if restart_count >= 1:
                mgr._stopping = True

        mgr._disconnect_ipc = AsyncMock()
        mgr.start = fake_start

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr._health_loop()

        assert mgr._restart_backoff == 60.0

    async def test_health_loop_handles_cancellation(self):
        mgr = _make_manager()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = asyncio.CancelledError
            # Should exit gracefully
            await mgr._health_loop()

    async def test_health_loop_skips_retry_when_user_stopped(self):
        mgr = _make_manager()
        mgr._config.stream.url = "http://example.com/stream.m3u8"
        _attach_mock_process(mgr, alive=True)
        mgr._user_stopped = True
        mgr._last_stream_attempt = 0.0
        mgr._stream_retry_backoff = 5.0

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mgr._stopping = True

        mgr._get_property = AsyncMock(return_value="mpv 0.38.0")
        mgr.get_status = AsyncMock(return_value={"idle": True})
        mgr.set_overlay = AsyncMock()
        mgr.load_stream = AsyncMock()

        with patch("asyncio.sleep", side_effect=fake_sleep), \
             patch("asyncio.wait_for", new_callable=AsyncMock), \
             patch("pi_decoder.mpv_manager._get_network_info", return_value={
                 "connection_type": "ethernet",
                 "ip": "192.168.1.1",
                 "ssid": "",
                 "hotspot_active": False,
                 "signal": 0,
             }):
            await mgr._health_loop()

        mgr.load_stream.assert_not_awaited()

    async def test_health_loop_reloads_stream_when_idle(self):
        mgr = _make_manager()
        mgr._config.stream.url = "http://example.com/stream.m3u8"
        _attach_mock_process(mgr, alive=True)
        mgr._last_stream_attempt = 0.0
        mgr._stream_retry_backoff = 5.0

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mgr._stopping = True

        mgr._get_property = AsyncMock(return_value="mpv 0.38.0")
        mgr.get_status = AsyncMock(return_value={"idle": True})
        mgr.set_overlay = AsyncMock()
        mgr.load_stream = AsyncMock()

        with patch("asyncio.sleep", side_effect=fake_sleep), \
             patch("asyncio.wait_for", new_callable=AsyncMock), \
             patch("pi_decoder.mpv_manager._get_network_info", return_value={
                 "connection_type": "ethernet",
                 "ip": "192.168.1.1",
                 "ssid": "",
                 "hotspot_active": False,
                 "signal": 0,
             }):
            await mgr._health_loop()

        mgr.load_stream.assert_awaited_with("http://example.com/stream.m3u8")

    async def test_health_loop_removes_overlay_when_playing(self):
        mgr = _make_manager()
        _attach_mock_process(mgr, alive=True)

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mgr._stopping = True

        mgr._get_property = AsyncMock(return_value="mpv 0.38.0")
        mgr.get_status = AsyncMock(return_value={"idle": False, "playing": True})
        mgr.remove_overlay = AsyncMock()

        with patch("asyncio.sleep", side_effect=fake_sleep), \
             patch("asyncio.wait_for", new_callable=AsyncMock), \
             patch("pi_decoder.mpv_manager._get_network_info", return_value={
                 "connection_type": "ethernet",
                 "ip": "192.168.1.1",
                 "ssid": "",
                 "hotspot_active": False,
                 "signal": 0,
             }):
            await mgr._health_loop()

        mgr.remove_overlay.assert_awaited_with(IP_OVERLAY_ID)
        # Backoff should be reset when playing
        assert mgr._stream_retry_backoff == 5.0

    async def test_health_loop_increases_stream_backoff(self):
        mgr = _make_manager()
        mgr._config.stream.url = "http://example.com/stream.m3u8"
        _attach_mock_process(mgr, alive=True)
        mgr._last_stream_attempt = 0.0
        mgr._stream_retry_backoff = 5.0

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mgr._stopping = True

        mgr._get_property = AsyncMock(return_value="mpv 0.38.0")
        mgr.get_status = AsyncMock(return_value={"idle": True})
        mgr.set_overlay = AsyncMock()
        mgr.load_stream = AsyncMock()

        with patch("asyncio.sleep", side_effect=fake_sleep), \
             patch("asyncio.wait_for", new_callable=AsyncMock), \
             patch("pi_decoder.mpv_manager._get_network_info", return_value={
                 "connection_type": "ethernet",
                 "ip": "192.168.1.1",
                 "ssid": "",
                 "hotspot_active": False,
                 "signal": 0,
             }):
            await mgr._health_loop()

        # After one reload attempt: min(5.0 * 1.5, 60.0) = 7.5
        assert mgr._stream_retry_backoff == 7.5

    async def test_health_loop_resets_retry_on_network_change(self):
        mgr = _make_manager()
        mgr._config.stream.url = "http://example.com/stream.m3u8"
        _attach_mock_process(mgr, alive=True)
        mgr._last_connection_type = "hotspot"
        mgr._stream_retry_backoff = 30.0
        # Set last_stream_attempt far in the past so the backoff check wouldn't
        # normally pass with 30s backoff, but after reset (5s) it will.
        mgr._last_stream_attempt = time.monotonic() - 10.0

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mgr._stopping = True

        mgr._get_property = AsyncMock(return_value="mpv 0.38.0")
        mgr.get_status = AsyncMock(return_value={"idle": True})
        mgr.set_overlay = AsyncMock()
        mgr.load_stream = AsyncMock()

        with patch("asyncio.sleep", side_effect=fake_sleep), \
             patch("asyncio.wait_for", new_callable=AsyncMock), \
             patch("pi_decoder.mpv_manager._get_network_info", return_value={
                 "connection_type": "ethernet",
                 "ip": "192.168.1.1",
                 "ssid": "",
                 "hotspot_active": False,
                 "signal": 0,
             }):
            await mgr._health_loop()

        # Network changed from hotspot -> ethernet, reset_stream_retry was called
        # (backoff went from 30.0 -> 5.0), then stream reload happened (5.0 * 1.5 = 7.5)
        assert mgr._stream_retry_backoff == 7.5
        mgr.load_stream.assert_awaited()
        assert mgr._last_connection_type == "ethernet"
