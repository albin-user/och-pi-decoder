"""Tests for the web API endpoints."""

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from pi_decoder.config import Config
from pi_decoder.web.app import create_app


@pytest.fixture
def config():
    """Return a default test config."""
    cfg = Config()
    cfg.general.name = "Test-Decoder"
    cfg.stream.url = "rtmp://test.local/live"
    cfg.network.hotspot_ssid = "Test-Hotspot"
    cfg.network.hotspot_password = "testpassword"
    return cfg


@pytest.fixture
def mock_mpv():
    """Return a mocked MpvManager."""
    mpv = MagicMock()
    mpv.get_status = AsyncMock(return_value={
        "alive": True,
        "playing": True,
        "idle": False,
        "stream_url": "rtmp://test.local/live",
    })
    mpv.restart = AsyncMock()
    mpv.reset_stream_retry = MagicMock()
    mpv.take_screenshot = AsyncMock(return_value=None)
    mpv.stop_stream = AsyncMock()
    mpv.load_stream = AsyncMock()
    return mpv


@pytest.fixture
def mock_overlay():
    """Return a mocked OverlayUpdater."""
    overlay = MagicMock()
    overlay.running = False
    overlay.last_status = MagicMock(
        is_live=False, finished=False, plan_title="", item_title="",
        service_end_time=None, message="",
    )
    overlay.stop = AsyncMock()
    overlay.start_task = MagicMock()
    return overlay


@pytest.fixture
def mock_pco():
    """Return a mocked PCOClient."""
    pco = MagicMock()
    pco.update_credentials = MagicMock()
    pco.credential_error = ""
    pco.consecutive_failures = 0
    pco.get_service_types = AsyncMock(return_value=[])
    pco.test_connection = AsyncMock(return_value={"success": True, "service_types": []})
    pco.close = AsyncMock()
    return pco


@pytest.fixture
def client(config, mock_mpv, mock_overlay, mock_pco, tmp_path):
    """Return a TestClient for the app."""
    config_path = str(tmp_path / "config.toml")
    app = create_app(mock_mpv, mock_pco, mock_overlay, config, config_path)
    return TestClient(app)


class TestIndexPage:
    def test_get_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Test-Decoder" in resp.text


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestStatusEndpoint:
    @patch("pi_decoder.network.get_network_info_sync", return_value={})
    def test_status_returns_json(self, _mock_net, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "mpv" in data
        assert "system" in data
        assert data["name"] == "Test-Decoder"


class TestVersionEndpoint:
    def test_version_returns_string(self, client):
        resp = client.get("/api/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data


class TestStreamConfig:
    def test_save_stream_config_valid(self, client, config):
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://new.local/live",
            "network_caching": 3000,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.stream.url == "rtmp://new.local/live"
        assert config.stream.network_caching == 3000


class TestStreamConfigHwdec:
    def test_save_hwdec_valid(self, client, config):
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://test.local/live",
            "network_caching": 2000,
            "hwdec": "v4l2m2m",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.stream.hwdec == "v4l2m2m"

    def test_save_hwdec_invalid_rejected(self, client, config):
        config.stream.hwdec = "auto"
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://test.local/live",
            "network_caching": 2000,
            "hwdec": "cuda",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "Invalid hwdec" in data["error"]
        # Should not have changed
        assert config.stream.hwdec == "auto"

    def test_save_hwdec_omitted_keeps_existing(self, client, config):
        config.stream.hwdec = "v4l2m2m"
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://test.local/live",
            "network_caching": 2000,
        })
        assert resp.status_code == 200
        assert config.stream.hwdec == "v4l2m2m"


class TestGeneralConfig:
    def test_save_name(self, client, config):
        with patch("pi_decoder.hostname.set_hostname", new_callable=AsyncMock, return_value="new-name"):
            resp = client.post("/api/config/general", json={"name": "New Name"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.general.name == "New Name"


class TestPcoConfig:
    def test_save_pco_credentials(self, client, config):
        resp = client.post("/api/config/pco", json={
            "app_id": "test_id",
            "secret": "test_secret",
            "service_type_id": "123",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.pco.app_id == "test_id"

    def test_test_pco_missing_credentials(self, client):
        resp = client.post("/api/test-pco", json={
            "app_id": "",
            "secret": "",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False


class TestLogsEndpoint:
    @patch("subprocess.run")
    def test_logs_valid_service(self, mock_run, client):
        mock_run.return_value = MagicMock(stdout="test log output")
        resp = client.get("/api/logs?service=pi-decoder&lines=25")
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data

    def test_logs_invalid_service(self, client):
        resp = client.get("/api/logs?service=malicious-service")
        assert resp.status_code == 400
        assert "error" in resp.json()


class TestWifiConnect:
    def test_wifi_connect_validates_ssid_length(self, client):
        # SSID > 32 bytes
        long_ssid = "A" * 33
        resp = client.post("/api/network/wifi-connect", json={
            "ssid": long_ssid,
            "password": "testpassword",
        })
        assert resp.status_code == 400

    def test_wifi_connect_validates_password_length(self, client):
        resp = client.post("/api/network/wifi-connect", json={
            "ssid": "TestNetwork",
            "password": "short",
        })
        assert resp.status_code == 400
        assert "8-63" in resp.json()["error"]

    def test_wifi_connect_validates_empty_ssid(self, client):
        resp = client.post("/api/network/wifi-connect", json={
            "ssid": "",
            "password": "testpassword",
        })
        assert resp.status_code == 400


class TestCecInput:
    def test_cec_input_invalid_port(self, client):
        resp = client.post("/api/cec/input", json={"port": "not_a_number"})
        assert resp.status_code == 400
        assert "Invalid port" in resp.json()["error"]


class TestConfigExport:
    def test_export_returns_toml(self, client):
        resp = client.get("/api/config/export")
        assert resp.status_code == 200
        assert "toml" in resp.headers["content-type"]


class TestScreenshot:
    def test_screenshot_success(self, client, mock_mpv):
        mock_mpv.take_screenshot = AsyncMock(return_value=b"\xff\xd8fake-jpeg-data")
        resp = client.get("/api/screenshot")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == b"\xff\xd8fake-jpeg-data"

    def test_screenshot_failure_returns_500(self, client, mock_mpv):
        mock_mpv.take_screenshot = AsyncMock(return_value=None)
        resp = client.get("/api/screenshot")
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False
        assert data["error"] == "Screenshot failed"


class TestWifiScan:
    @patch("pi_decoder.network.scan_wifi", new_callable=AsyncMock, return_value=[
        {"ssid": "Network1", "signal": -45},
        {"ssid": "Network2", "signal": -70},
    ])
    def test_wifi_scan_returns_networks(self, _mock_scan, client):
        resp = client.get("/api/network/wifi-scan")
        assert resp.status_code == 200
        data = resp.json()
        assert "networks" in data
        assert len(data["networks"]) == 2
        assert data["networks"][0]["ssid"] == "Network1"


class TestNetworkStatus:
    @patch("pi_decoder.network.get_network_info_sync", return_value={
        "ip": "192.168.1.100",
        "interface": "wlan0",
        "ssid": "MyWifi",
        "hotspot_active": False,
    })
    def test_network_status_returns_info(self, _mock_net, client):
        resp = client.get("/api/network/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ip"] == "192.168.1.100"
        assert data["interface"] == "wlan0"


class TestCecEndpoints:
    @patch("pi_decoder.cec.power_on", new_callable=AsyncMock, return_value="ok")
    def test_cec_on(self, _mock, client):
        resp = client.post("/api/cec/on")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("pi_decoder.cec.standby", new_callable=AsyncMock, return_value="ok")
    def test_cec_standby(self, _mock, client):
        resp = client.post("/api/cec/standby")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("pi_decoder.cec.volume_up", new_callable=AsyncMock, return_value="ok")
    def test_cec_volume_up(self, _mock, client):
        resp = client.post("/api/cec/volume-up")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("pi_decoder.cec.volume_down", new_callable=AsyncMock, return_value="ok")
    def test_cec_volume_down(self, _mock, client):
        resp = client.post("/api/cec/volume-down")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("pi_decoder.cec.mute", new_callable=AsyncMock, return_value="ok")
    def test_cec_mute(self, _mock, client):
        resp = client.post("/api/cec/mute")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("pi_decoder.cec.active_source", new_callable=AsyncMock, return_value="ok")
    def test_cec_active_source(self, _mock, client):
        resp = client.post("/api/cec/active-source")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock, return_value="on")
    def test_cec_power_status(self, _mock, client):
        resp = client.get("/api/cec/power-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "on"


class TestConfigImport:
    def test_import_valid_toml(self, client, config):
        toml_content = b'[general]\nname = "Imported-Decoder"\n'
        resp = client.post(
            "/api/config/import",
            files={"file": ("config.toml", toml_content, "application/toml")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["message"] == "Config imported successfully"
        assert config.general.name == "Imported-Decoder"

    def test_import_invalid_toml(self, client):
        bad_content = b"this is [not valid toml ==="
        resp = client.post(
            "/api/config/import",
            files={"file": ("config.toml", bad_content, "application/toml")},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "Invalid TOML" in data["error"]

    def test_import_oversized_file(self, client):
        big_content = b"x" * (65 * 1024)  # 65 KB, over the 64KB limit
        resp = client.post(
            "/api/config/import",
            files={"file": ("config.toml", big_content, "application/toml")},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "too large" in data["error"]

    def test_import_wrong_extension(self, client):
        content = b'[general]\nname = "Test"\n'
        resp = client.post(
            "/api/config/import",
            files={"file": ("config.json", content, "application/json")},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert ".toml" in data["error"]

    def test_import_calls_validate_config(self, client, config):
        toml_content = b'[stream]\nurl = "rtmp://imported.local/live"\n'
        with patch("pi_decoder.web.app.validate_config") as mock_validate:
            resp = client.post(
                "/api/config/import",
                files={"file": ("config.toml", toml_content, "application/toml")},
            )
        assert resp.status_code == 200
        # Called twice: once for pre-validation rollback check, once for final save
        assert mock_validate.call_count == 2
        mock_validate.assert_called_with(config)


class TestRestartEndpoints:
    def test_restart_video(self, client, mock_mpv):
        resp = client.post("/api/restart/video")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_restart_overlay(self, client, mock_overlay):
        resp = client.post("/api/restart/overlay")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_overlay.stop.assert_awaited_once()
        mock_overlay.start_task.assert_called_once()

    def test_restart_all(self, client, mock_mpv, mock_overlay):
        resp = client.post("/api/restart/all")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_overlay.stop.assert_awaited_once()


class TestLazyOverlayCreation:
    def test_restart_overlay_creates_lazily(self, config, mock_mpv, tmp_path):
        """When pco=None and overlay=None, restart/overlay should create them."""
        config.overlay.enabled = True
        config.pco.app_id = "test_id"
        config.pco.secret = "test_secret"
        config_path = str(tmp_path / "config.toml")
        app = create_app(mock_mpv, None, None, config, config_path)
        with patch("pi_decoder.web.app.PCOClient") as MockPCO, \
             patch("pi_decoder.web.app.OverlayUpdater") as MockOverlay:
            mock_ov = MagicMock()
            mock_ov.stop = AsyncMock()
            mock_ov.start_task = MagicMock()
            MockOverlay.return_value = mock_ov
            MockPCO.return_value = MagicMock()
            c = TestClient(app)
            resp = c.post("/api/restart/overlay")
        assert resp.status_code == 200
        MockPCO.assert_called_once_with(config)
        MockOverlay.assert_called_once()
        mock_ov.stop.assert_awaited_once()
        mock_ov.start_task.assert_called_once()

    def test_pco_config_creates_pco_lazily(self, config, mock_mpv, tmp_path):
        """Saving PCO credentials should lazily create PCOClient."""
        config.overlay.enabled = True
        config_path = str(tmp_path / "config.toml")
        app = create_app(mock_mpv, None, None, config, config_path)
        with patch("pi_decoder.web.app.PCOClient") as MockPCO, \
             patch("pi_decoder.web.app.OverlayUpdater") as MockOverlay:
            mock_pco_inst = MagicMock()
            mock_pco_inst.update_credentials = MagicMock()
            MockPCO.return_value = mock_pco_inst
            MockOverlay.return_value = MagicMock()
            c = TestClient(app)
            resp = c.post("/api/config/pco", json={
                "app_id": "new_id",
                "secret": "new_secret",
                "service_type_id": "123",
            })
        assert resp.status_code == 200
        MockPCO.assert_called_once_with(config)
        mock_pco_inst.update_credentials.assert_called_once()

    def test_overlay_disabled_does_not_create(self, config, mock_mpv, tmp_path):
        """When overlay is disabled, _ensure_overlay_created should not create anything."""
        config.overlay.enabled = False
        config.pco.app_id = "test_id"
        config_path = str(tmp_path / "config.toml")
        app = create_app(mock_mpv, None, None, config, config_path)
        with patch("pi_decoder.web.app.PCOClient") as MockPCO, \
             patch("pi_decoder.web.app.OverlayUpdater") as MockOverlay:
            c = TestClient(app)
            resp = c.post("/api/restart/overlay")
        assert resp.status_code == 200
        MockPCO.assert_not_called()
        MockOverlay.assert_not_called()

    def test_pco_config_starts_overlay_after_creation(self, config, mock_mpv, tmp_path):
        """Saving PCO credentials should start the overlay if it was lazily created."""
        config.overlay.enabled = True
        config_path = str(tmp_path / "config.toml")
        app = create_app(mock_mpv, None, None, config, config_path)
        with patch("pi_decoder.web.app.PCOClient") as MockPCO, \
             patch("pi_decoder.web.app.OverlayUpdater") as MockOverlay:
            mock_pco_inst = MagicMock()
            mock_pco_inst.update_credentials = MagicMock()
            MockPCO.return_value = mock_pco_inst
            mock_ov = MagicMock()
            mock_ov.running = False
            mock_ov.start_task = MagicMock()
            MockOverlay.return_value = mock_ov
            c = TestClient(app)
            resp = c.post("/api/config/pco", json={
                "app_id": "new_id",
                "secret": "new_secret",
                "service_type_id": "123",
            })
        assert resp.status_code == 200
        MockOverlay.assert_called_once()
        mock_pco_inst.update_credentials.assert_called_once()
        mock_ov.start_task.assert_called_once()

    def test_overlay_config_stops_when_disabled(self, config, mock_mpv, mock_overlay, mock_pco, tmp_path):
        """Saving overlay config with enabled=False should stop existing overlay."""
        config.overlay.enabled = True
        config_path = str(tmp_path / "config.toml")
        app = create_app(mock_mpv, mock_pco, mock_overlay, config, config_path)
        c = TestClient(app)
        resp = c.post("/api/config/overlay", json={"enabled": False})
        assert resp.status_code == 200
        mock_overlay.stop.assert_awaited_once()


class TestStopVideo:
    def test_stop_video(self, client, mock_mpv):
        mock_mpv.stop_stream = AsyncMock()
        resp = client.post("/api/stop/video")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_mpv.stop_stream.assert_awaited_once()


class TestWebSocket:
    @patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock, return_value="on")
    @patch("pi_decoder.network.get_network_info_sync", return_value={
        "ip": "10.42.0.1",
        "hotspot_active": True,
        "hotspot_password": "secret123",
    })
    def test_ws_status_sends_json_shape(self, _mock_net, _mock_cec, client):
        with client.websocket_connect("/ws/status") as ws:
            data = ws.receive_json()
        assert "name" in data
        assert "hostname" in data
        assert "mpv" in data
        assert "overlay" in data
        assert "system" in data
        assert "network" in data
        assert "cec" in data
        assert data["name"] == "Test-Decoder"

    @patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock, return_value="on")
    @patch("pi_decoder.network.get_network_info_sync", return_value={
        "ip": "10.42.0.1",
        "hotspot_active": True,
        "hotspot_password": "secret123",
    })
    def test_ws_status_strips_hotspot_password(self, _mock_net, _mock_cec, client):
        with client.websocket_connect("/ws/status") as ws:
            data = ws.receive_json()
        assert "hotspot_password" not in data["network"]

    @patch("pi_decoder.cec.is_available", return_value=True)
    @patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock, return_value="standby")
    @patch("pi_decoder.network.get_network_info_sync", return_value={})
    def test_ws_status_includes_cec_power(self, _mock_net, _mock_cec, _mock_avail, client):
        with client.websocket_connect("/ws/status") as ws:
            data = ws.receive_json()
        assert data["cec"]["power"] == "standby"


class TestSpeedTest:
    @patch("pi_decoder.network.load_speed_test_result", return_value={
        "download_mbps": 47.2, "latency_ms": 12.0, "timestamp": "2025-01-15T10:30:00",
    })
    def test_get_returns_last_result(self, _mock, client):
        resp = client.get("/api/network/speedtest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["result"]["download_mbps"] == 47.2

    @patch("pi_decoder.network.load_speed_test_result", return_value=None)
    def test_get_returns_null_when_no_result(self, _mock, client):
        resp = client.get("/api/network/speedtest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["result"] is None

    @patch("pi_decoder.network.run_speed_test", new_callable=AsyncMock, return_value={
        "download_mbps": 50.0, "latency_ms": 8.5, "timestamp": "2025-01-15T10:30:00",
        "wifi_band": None, "avg_signal": None, "interface_type": None,
    })
    def test_post_returns_results(self, _mock, client):
        resp = client.post("/api/network/speedtest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["download_mbps"] == 50.0

    @patch("pi_decoder.network.run_speed_test", new_callable=AsyncMock,
           side_effect=RuntimeError("Speed test already in progress"))
    def test_post_409_on_concurrent(self, _mock, client):
        resp = client.post("/api/network/speedtest")
        assert resp.status_code == 409
        data = resp.json()
        assert data["ok"] is False
        assert "already in progress" in data["error"]

    @patch("pi_decoder.network.run_speed_test", new_callable=AsyncMock,
           side_effect=Exception("Connection failed"))
    def test_post_500_on_failure(self, _mock, client):
        resp = client.post("/api/network/speedtest")
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False


class TestOverlayConfig:
    def test_save_overlay_config(self, client, config):
        resp = client.post("/api/config/overlay", json={
            "enabled": True,
            "position": "top-left",
            "font_size": 72,
            "font_size_title": 30,
            "font_size_info": 24,
            "transparency": 0.5,
            "timer_mode": "item",
            "show_description": True,
            "show_service_end": False,
            "timezone": "America/New_York",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.overlay.enabled is True
        assert config.overlay.position == "top-left"
        assert config.overlay.font_size == 72


class TestNetworkConfig:
    def test_save_network_config(self, client, config):
        resp = client.post("/api/config/network", json={
            "hotspot_ssid": "NewHotspot",
            "hotspot_password": "newpassword",
            "ethernet_timeout": 15,
            "wifi_timeout": 30,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.network.hotspot_ssid == "NewHotspot"
        assert config.network.ethernet_timeout == 15


class TestServiceTypes:
    def test_get_service_types(self, client, mock_pco):
        mock_pco.get_service_types = AsyncMock(return_value=[
            {"id": "1", "name": "Sunday"},
        ])
        resp = client.get("/api/service-types")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["service_types"]) == 1

    def test_get_service_types_no_pco(self, config, mock_mpv, mock_overlay, tmp_path):
        config_path = str(tmp_path / "config.toml")
        app = create_app(mock_mpv, None, mock_overlay, config, config_path)
        c = TestClient(app)
        resp = c.get("/api/service-types")
        assert resp.status_code == 200
        assert resp.json()["service_types"] == []


class TestHotspotGuard:
    @patch("pi_decoder.network.start_hotspot", new_callable=AsyncMock)
    @patch("pi_decoder.network.get_network_info_sync", return_value={
        "connection_type": "ethernet", "hotspot_active": False,
    })
    def test_hotspot_rejected_when_ethernet(self, _mock_net, _mock_start, client):
        resp = client.post("/api/network/hotspot/start")
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "ethernet" in data["error"]
        _mock_start.assert_not_awaited()

    @patch("pi_decoder.network.start_hotspot", new_callable=AsyncMock)
    @patch("pi_decoder.network.get_network_info_sync", return_value={
        "connection_type": "wifi", "hotspot_active": False,
    })
    def test_hotspot_rejected_when_wifi(self, _mock_net, _mock_start, client):
        resp = client.post("/api/network/hotspot/start")
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "wifi" in data["error"]

    @patch("pi_decoder.network.start_hotspot", new_callable=AsyncMock)
    @patch("pi_decoder.network.get_network_info_sync", return_value={
        "connection_type": "none", "hotspot_active": False,
    })
    def test_hotspot_allowed_when_disconnected(self, _mock_net, _mock_start, client):
        resp = client.post("/api/network/hotspot/start")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestStaticIpConfig:
    def test_save_static_ip_config(self, client, config):
        resp = client.post("/api/config/network", json={
            "eth_ip_mode": "manual",
            "eth_ip_address": "192.168.1.100/24",
            "eth_gateway": "192.168.1.1",
            "eth_dns": "8.8.8.8",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.network.eth_ip_mode == "manual"
        assert config.network.eth_ip_address == "192.168.1.100/24"

    @patch("pi_decoder.network.apply_static_ip", new_callable=AsyncMock,
           return_value="IP manual applied to Wired connection 1")
    def test_apply_ip_success(self, _mock_apply, client):
        resp = client.post("/api/network/apply-ip", json={"interface": "ethernet"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "applied" in data["message"]

    @patch("pi_decoder.network.apply_static_ip", new_callable=AsyncMock,
           side_effect=RuntimeError("No active ethernet connection"))
    def test_apply_ip_no_connection(self, _mock_apply, client):
        resp = client.post("/api/network/apply-ip", json={"interface": "ethernet"})
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False
        assert "No active" in data["error"]

    def test_apply_ip_invalid_interface(self, client):
        resp = client.post("/api/network/apply-ip", json={"interface": "hotspot"})
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "Invalid interface" in data["error"]


class TestHotspotEndpoints:
    @patch("pi_decoder.network.start_hotspot", new_callable=AsyncMock)
    @patch("pi_decoder.network.get_network_info_sync", return_value={
        "connection_type": "none", "hotspot_active": False,
    })
    def test_start_hotspot(self, _mock_net, _mock_start, client):
        resp = client.post("/api/network/hotspot/start")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("pi_decoder.network.stop_hotspot", new_callable=AsyncMock)
    def test_stop_hotspot(self, _mock, client):
        resp = client.post("/api/network/hotspot/stop")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestSavedNetworks:
    @patch("pi_decoder.network.get_saved_networks", new_callable=AsyncMock, return_value=["HomeWifi", "Office"])
    def test_get_saved_networks(self, _mock, client):
        resp = client.get("/api/network/wifi/saved")
        assert resp.status_code == 200
        data = resp.json()
        assert data["networks"] == ["HomeWifi", "Office"]


class TestForgetNetwork:
    @patch("pi_decoder.network.forget_network", new_callable=AsyncMock)
    def test_forget_success(self, _mock, client):
        resp = client.post("/api/network/wifi/forget", json={"name": "OldWifi"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_forget_empty_name(self, client):
        resp = client.post("/api/network/wifi/forget", json={"name": ""})
        assert resp.status_code == 400
        assert resp.json()["ok"] is False


class TestRebootShutdown:
    @patch("subprocess.Popen")
    def test_reboot(self, _mock_popen, client):
        resp = client.post("/api/reboot")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("subprocess.Popen")
    def test_shutdown(self, _mock_popen, client):
        resp = client.post("/api/shutdown")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestStreamMaxResolution:
    def test_save_max_resolution_valid(self, client, config):
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://test.local/live",
            "network_caching": 2000,
            "max_resolution": "720",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.stream.max_resolution == "720"

    def test_save_max_resolution_invalid_rejected(self, client, config):
        config.stream.max_resolution = "1080"
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://test.local/live",
            "network_caching": 2000,
            "max_resolution": "4k",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "Invalid max_resolution" in data["error"]
        assert config.stream.max_resolution == "1080"

    def test_save_max_resolution_omitted_keeps_existing(self, client, config):
        config.stream.max_resolution = "720"
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://test.local/live",
            "network_caching": 2000,
        })
        assert resp.status_code == 200
        assert config.stream.max_resolution == "720"

    def test_save_max_resolution_best(self, client, config):
        resp = client.post("/api/config/stream", json={
            "url": "rtmp://test.local/live",
            "network_caching": 2000,
            "max_resolution": "best",
        })
        assert resp.status_code == 200
        assert config.stream.max_resolution == "best"


class TestDisplayModes:
    @patch("pi_decoder.display.get_current_resolution", return_value="1920x1080@60D")
    @patch("pi_decoder.display.get_available_modes", return_value=["1920x1080", "1280x720"])
    def test_get_display_modes(self, _mock_modes, _mock_current, client):
        resp = client.get("/api/display/modes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["modes"] == ["1920x1080", "1280x720"]
        assert data["current"] == "1920x1080@60D"


class TestDisplayResolution:
    @patch("pi_decoder.display.set_display_resolution", new_callable=AsyncMock)
    def test_set_resolution_valid(self, _mock_set, client, config):
        resp = client.post("/api/display/resolution", json={
            "resolution": "1280x720@60D",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert config.display.hdmi_resolution == "1280x720@60D"
        _mock_set.assert_awaited_once_with("1280x720@60D")

    def test_set_resolution_empty_rejected(self, client):
        resp = client.post("/api/display/resolution", json={
            "resolution": "",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "required" in data["error"]

    def test_set_resolution_invalid_format_rejected(self, client):
        resp = client.post("/api/display/resolution", json={
            "resolution": "invalid-res",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "Invalid resolution" in data["error"]

    @patch("pi_decoder.display.set_display_resolution", new_callable=AsyncMock,
           side_effect=RuntimeError("cmdline.txt not found"))
    def test_set_resolution_write_failure(self, _mock_set, client):
        resp = client.post("/api/display/resolution", json={
            "resolution": "1920x1080@60D",
        })
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False
        assert "Failed" in data["error"]


class TestCecErrorPaths:
    @patch("pi_decoder.cec.power_on", new_callable=AsyncMock, side_effect=Exception("CEC failed"))
    def test_cec_on_error(self, _mock, client):
        resp = client.post("/api/cec/on")
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False
        assert "CEC" in data["error"]


class TestStreamPresets:
    def test_get_presets_returns_list(self, client, config):
        config.stream.presets = [
            {"label": "Church", "url": "rtmp://church.local/live"},
            {"label": "Backup", "url": "rtmp://backup.local/live"},
        ]
        resp = client.get("/api/stream/presets")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["presets"]) == 2
        assert data["presets"][0]["label"] == "Church"

    def test_get_presets_empty(self, client, config):
        config.stream.presets = []
        resp = client.get("/api/stream/presets")
        assert resp.status_code == 200
        assert resp.json()["presets"] == []


class TestStreamPresetsSave:
    def test_save_presets_valid(self, client, config):
        resp = client.post("/api/stream/presets", json={
            "presets": [
                {"label": "Church", "url": "rtmp://church.local/live"},
                {"label": "Backup", "url": "rtmp://backup.local/live"},
            ],
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(config.stream.presets) == 2

    def test_save_presets_max_10_rejected(self, client):
        presets = [{"label": f"P{i}", "url": f"rtmp://host{i}/live"} for i in range(11)]
        resp = client.post("/api/stream/presets", json={"presets": presets})
        assert resp.status_code == 400
        assert "Max 10" in resp.json()["error"]

    def test_save_presets_drops_empty_label(self, client, config):
        resp = client.post("/api/stream/presets", json={
            "presets": [
                {"label": "", "url": "rtmp://host/live"},
                {"label": "Valid", "url": "rtmp://valid/live"},
            ],
        })
        assert resp.status_code == 200
        assert len(config.stream.presets) == 1
        assert config.stream.presets[0]["label"] == "Valid"

    def test_save_presets_truncates_label(self, client, config):
        long_label = "A" * 60
        resp = client.post("/api/stream/presets", json={
            "presets": [{"label": long_label, "url": "rtmp://host/live"}],
        })
        assert resp.status_code == 200
        assert len(config.stream.presets[0]["label"]) == 50


class TestStreamSwitch:
    def test_switch_success(self, client, config, mock_mpv):
        resp = client.post("/api/stream/switch", json={"url": "rtmp://new.local/live"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert config.stream.url == "rtmp://new.local/live"
        mock_mpv.restart.assert_awaited_once()

    def test_switch_empty_url(self, client):
        resp = client.post("/api/stream/switch", json={"url": ""})
        assert resp.status_code == 400
        assert "URL required" in resp.json()["error"]

    def test_switch_restart_failure(self, client, mock_mpv):
        mock_mpv.restart = AsyncMock(side_effect=RuntimeError("mpv crash"))
        resp = client.post("/api/stream/switch", json={"url": "rtmp://host/live"})
        assert resp.status_code == 500
        assert "restart failed" in resp.json()["error"]


class TestStreamSwitchBack:
    def test_switch_back_success(self, client, mock_mpv):
        resp = client.post("/api/stream/switch-back")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_mpv.reset_stream_retry.assert_called_once()
        mock_mpv.restart.assert_awaited_once()

    def test_switch_back_restart_failure(self, client, mock_mpv):
        mock_mpv.restart = AsyncMock(side_effect=RuntimeError("mpv crash"))
        resp = client.post("/api/stream/switch-back")
        assert resp.status_code == 500
        assert "failed" in resp.json()["error"]


class TestNetworkPing:
    @patch("subprocess.run")
    def test_ping_success(self, mock_run, client):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="PING host (1.2.3.4): 3 packets\nrtt min/avg/max = 5.0/10.5/15.0 ms",
        )
        resp = client.post("/api/network/ping", json={"host": "example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["reachable"] is True
        assert data["avg_ms"] == 10.5

    def test_ping_invalid_hostname(self, client):
        resp = client.post("/api/network/ping", json={"host": "bad;host"})
        assert resp.status_code == 400
        assert "Invalid hostname" in resp.json()["error"]

    def test_ping_falls_back_to_stream_url(self, client, config):
        config.stream.url = "rtmp://stream.example.com/live"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="rtt min/avg/max = 1.0/2.0/3.0 ms",
            )
            resp = client.post("/api/network/ping", json={"host": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["host"] == "stream.example.com"

    def test_ping_no_host_no_url(self, client, config):
        config.stream.url = ""
        resp = client.post("/api/network/ping", json={"host": ""})
        assert resp.status_code == 400
        assert "No host" in resp.json()["error"]

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ping", timeout=15))
    def test_ping_timeout(self, _mock, client):
        resp = client.post("/api/network/ping", json={"host": "slow.example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is False
        assert "timed out" in data["output"].lower()


class TestLogsDownload:
    @patch("socket.gethostname", return_value="test-decoder")
    @patch("subprocess.run")
    def test_download_returns_text_file(self, mock_run, _mock_host, client):
        mock_run.return_value = MagicMock(stdout="log line 1\nlog line 2\n")
        resp = client.get("/api/logs/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        assert 'test-decoder-pi-decoder-logs.txt' in resp.headers["content-disposition"]
        assert "log line 1" in resp.text


class TestRestartVideoError:
    def test_restart_video_error(self, client, mock_mpv):
        mock_mpv.restart = AsyncMock(side_effect=RuntimeError("mpv segfault"))
        resp = client.post("/api/restart/video")
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False
        assert "mpv segfault" in data["error"]
