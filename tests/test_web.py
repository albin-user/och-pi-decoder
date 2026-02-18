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
        mock_validate.assert_called_once_with(config)


class TestSoftwareUpdate:
    def test_update_wrong_file_type(self, client):
        resp = client.post(
            "/api/update",
            files={"file": ("package.zip", b"fake", "application/zip")},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert ".whl" in data["error"] or ".tar.gz" in data["error"]

    def test_update_oversized_file(self, client):
        big_content = b"\x00" * (11 * 1024 * 1024)  # 11 MB, over the 10 MB limit
        resp = client.post(
            "/api/update",
            files={"file": ("pkg.whl", big_content, "application/octet-stream")},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "too large" in data["error"]

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_update_successful_install(self, mock_run, _mock_popen, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="Successfully installed", stderr="")
        resp = client.post(
            "/api/update",
            files={"file": ("pi_decoder-1.2.3.whl", b"fake-wheel-data", "application/octet-stream")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "version" in data
        assert "message" in data


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

    @patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock, return_value="standby")
    @patch("pi_decoder.network.get_network_info_sync", return_value={})
    def test_ws_status_includes_cec_power(self, _mock_net, _mock_cec, client):
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


class TestCecErrorPaths:
    @patch("pi_decoder.cec.power_on", new_callable=AsyncMock, side_effect=Exception("CEC failed"))
    def test_cec_on_error(self, _mock, client):
        resp = client.post("/api/cec/on")
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False
        assert "CEC" in data["error"]
