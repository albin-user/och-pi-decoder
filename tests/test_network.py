"""Tests for network management module."""

import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock
import subprocess

import pytest

from pi_decoder import network
from pi_decoder.network import run_speed_test, load_speed_test_result, SPEEDTEST_RESULT_PATH


def _make_sync_result(stdout: str = "", returncode: int = 0):
    """Create a mock subprocess.run result."""
    return MagicMock(stdout=stdout, stderr="", returncode=returncode)


class TestGetNetworkInfoSync:

    def test_ethernet_connection(self):
        device_output = "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:disconnected:\n"
        ip_output = "192.168.1.42/24\n"

        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if "device" in cmd and "show" in cmd:
                    return _make_sync_result(ip_output)
                return _make_sync_result(device_output)
            mock_run.side_effect = side_effect

            info = network.get_network_info_sync()

        assert info["connection_type"] == "ethernet"
        assert info["ip"] == "192.168.1.42"

    def test_wifi_connection(self):
        device_output = "eth0:ethernet:disconnected:\nwlan0:wifi:connected:MyWiFi\n"
        ip_output = "10.0.0.5/24\n"
        signal_output = "yes:72\nno:45\n"

        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                cmd_str = " ".join(cmd)
                if "ACTIVE,SIGNAL" in cmd_str:
                    return _make_sync_result(signal_output)
                if "device" in cmd and "show" in cmd:
                    return _make_sync_result(ip_output)
                return _make_sync_result(device_output)
            mock_run.side_effect = side_effect

            info = network.get_network_info_sync()

        assert info["connection_type"] == "wifi"
        assert info["ssid"] == "MyWiFi"
        assert info["signal"] == 72

    def test_hotspot_mode(self):
        device_output = "wlan0:wifi:connected:Hotspot\n"
        ip_output = "10.42.0.1/24\n"

        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if "device" in cmd and "show" in cmd:
                    return _make_sync_result(ip_output)
                return _make_sync_result(device_output)
            mock_run.side_effect = side_effect

            info = network.get_network_info_sync()

        assert info["connection_type"] == "hotspot"
        assert info["hotspot_active"] is True
        assert info["ip"] == "10.42.0.1"

    def test_ethernet_prioritized_over_hotspot(self):
        """When both ethernet and wifi/hotspot are connected, ethernet wins."""
        device_output = "wlan0:wifi:connected:Hotspot\neth0:ethernet:connected:Wired connection 1\n"
        ip_output = "192.168.1.50/24\n"

        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if "device" in cmd and "show" in cmd:
                    return _make_sync_result(ip_output)
                return _make_sync_result(device_output)
            mock_run.side_effect = side_effect

            info = network.get_network_info_sync()

        assert info["connection_type"] == "ethernet"
        assert info["ip"] == "192.168.1.50"
        assert info["hotspot_active"] is True

    def test_no_connection_fallback(self):
        device_output = "eth0:ethernet:disconnected:\nwlan0:wifi:disconnected:\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_sync_result(device_output)

            # Also mock socket fallback to fail
            with patch("socket.socket") as mock_sock:
                mock_sock.return_value.__enter__ = lambda self: self
                mock_sock.return_value.__exit__ = MagicMock(return_value=False)
                mock_sock.return_value.connect.side_effect = Exception("no net")
                info = network.get_network_info_sync()

        assert info["connection_type"] == "none"
        assert info["ip"] == ""


class TestScanWifi:

    @pytest.mark.asyncio
    async def test_scan_returns_sorted_list(self):
        scan_output = (
            "Network1:85:WPA2:*\n"
            "Network2:45:WPA2:\n"
            "Network3:72:OPEN:\n"
            "Network1:60:WPA2:\n"  # duplicate, lower signal — should be skipped
        )

        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock) as mock:
            mock.return_value = scan_output
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await network.scan_wifi()

        assert len(result) == 3
        assert result[0]["ssid"] == "Network1"
        assert result[0]["signal"] == 85
        assert result[0]["in_use"] is True
        assert result[1]["ssid"] == "Network3"
        assert result[2]["ssid"] == "Network2"

    @pytest.mark.asyncio
    async def test_scan_handles_empty(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock, return_value=""):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await network.scan_wifi()
        assert result == []

    @pytest.mark.asyncio
    async def test_scan_handles_failure(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    side_effect=RuntimeError("failed")):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await network.scan_wifi()
        assert result == []

    @pytest.mark.asyncio
    async def test_scan_skips_empty_ssid(self):
        scan_output = ":85:WPA2:\n--:45:WPA2:\nGoodNetwork:72:WPA2:\n"
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock, return_value=scan_output):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await network.scan_wifi()
        assert len(result) == 1
        assert result[0]["ssid"] == "GoodNetwork"


class TestConnectWifi:

    @pytest.mark.asyncio
    async def test_connect_success(self):
        with patch("pi_decoder.network.get_network_status", new_callable=AsyncMock,
                    return_value={"hotspot_active": False}):
            with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                        return_value="Connection activated"):
                result = await network.connect_wifi("TestSSID", "password123")
        assert "activated" in result.lower()

    @pytest.mark.asyncio
    async def test_connect_stops_hotspot_first(self):
        with patch("pi_decoder.network.get_network_status", new_callable=AsyncMock,
                    return_value={"hotspot_active": True}):
            with patch("pi_decoder.network.stop_hotspot", new_callable=AsyncMock) as mock_stop:
                with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                            return_value="Connected"):
                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        await network.connect_wifi("TestSSID", "pass")
        mock_stop.assert_called_once()


class TestHotspot:

    @pytest.mark.asyncio
    async def test_start_hotspot(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value="Hotspot started"):
            result = await network.start_hotspot("TestHotspot", "testpass")
        assert "started" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_hotspot(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value="Connection deactivated"):
            result = await network.stop_hotspot()
        assert "deactivated" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_hotspot_not_running(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    side_effect=RuntimeError("not active")):
            result = await network.stop_hotspot()
        assert result == "not running"


class TestSavedNetworks:

    @pytest.mark.asyncio
    async def test_get_saved_networks(self):
        output = "MyWiFi:802-11-wireless\nHotspot:802-11-wireless\nEthernet:802-3-ethernet\n"
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value=output):
            result = await network.get_saved_networks()
        assert result == ["MyWiFi"]
        assert "Hotspot" not in result

    @pytest.mark.asyncio
    async def test_get_saved_networks_empty(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value="Ethernet:802-3-ethernet\n"):
            result = await network.get_saved_networks()
        assert result == []

    @pytest.mark.asyncio
    async def test_forget_network(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value="Connection deleted") as mock:
            result = await network.forget_network("OldWiFi")
        assert "deleted" in result.lower()
        # Verify the right connection name was passed
        mock.assert_called_once_with("connection", "delete", "OldWiFi")


class TestGetActiveConnectionName:

    @pytest.mark.asyncio
    async def test_finds_ethernet(self):
        output = "Wired connection 1:802-3-ethernet\nHotspot:802-11-wireless\n"
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value=output):
            result = await network.get_active_connection_name("ethernet")
        assert result == "Wired connection 1"

    @pytest.mark.asyncio
    async def test_finds_wifi_skips_hotspot(self):
        output = "Hotspot:802-11-wireless\nMyWiFi:802-11-wireless\n"
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value=output):
            result = await network.get_active_connection_name("wifi")
        assert result == "MyWiFi"

    @pytest.mark.asyncio
    async def test_returns_empty_when_not_found(self):
        output = "Hotspot:802-11-wireless\n"
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value=output):
            result = await network.get_active_connection_name("ethernet")
        assert result == ""


class TestApplyStaticIp:

    @pytest.mark.asyncio
    async def test_apply_manual(self):
        calls = []

        async def mock_nmcli(*args):
            calls.append(args)
            if args[:2] == ("-t", "-f"):
                return "MyEthernet:802-3-ethernet\n"
            return "ok"

        with patch("pi_decoder.network._run_nmcli", side_effect=mock_nmcli):
            result = await network.apply_static_ip(
                "ethernet", "manual", "192.168.1.100/24", "192.168.1.1", "8.8.8.8, 1.1.1.1",
            )

        assert "applied" in result.lower()
        # Should have called modify with manual, then connection up
        modify_call = calls[1]
        assert "manual" in modify_call
        assert "192.168.1.100/24" in modify_call
        up_call = calls[2]
        assert up_call == ("connection", "up", "MyEthernet")

    @pytest.mark.asyncio
    async def test_apply_auto(self):
        calls = []

        async def mock_nmcli(*args):
            calls.append(args)
            if args[:2] == ("-t", "-f"):
                return "MyEthernet:802-3-ethernet\n"
            return "ok"

        with patch("pi_decoder.network._run_nmcli", side_effect=mock_nmcli):
            result = await network.apply_static_ip("ethernet", "auto")

        assert "applied" in result.lower()
        modify_call = calls[1]
        assert "auto" in modify_call

    @pytest.mark.asyncio
    async def test_no_active_connection_raises(self):
        with patch("pi_decoder.network._run_nmcli", new_callable=AsyncMock,
                    return_value="Hotspot:802-11-wireless\n"):
            with pytest.raises(RuntimeError, match="No active ethernet"):
                await network.apply_static_ip("ethernet", "manual", "192.168.1.100/24")


class TestGetIpForInterface:

    def test_returns_ip(self):
        with patch("subprocess.run", return_value=_make_sync_result("192.168.1.100/24\n")):
            result = network.get_ip_for_interface("eth0")
        assert result == "192.168.1.100"

    def test_skips_loopback(self):
        with patch("subprocess.run", return_value=_make_sync_result("127.0.0.1/8\n10.0.0.5/24\n")):
            result = network.get_ip_for_interface()
        assert result == "10.0.0.5"

    def test_returns_empty_on_failure(self):
        with patch("subprocess.run", side_effect=Exception("fail")):
            result = network.get_ip_for_interface()
        assert result == ""


class TestRunSpeedTest:

    @pytest.mark.asyncio
    async def test_success_returns_valid_result(self, tmp_path):
        """Successful test returns dict with expected keys."""
        fake_resp = MagicMock()
        fake_resp.content = b"\x00" * 10_000_000

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=fake_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        result_path = tmp_path / "speedtest.json"

        with patch("pi_decoder.network.SPEEDTEST_RESULT_PATH", result_path), \
             patch("pi_decoder.network.get_network_info_sync", return_value={"connection_type": "ethernet"}), \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = await run_speed_test()

        assert "download_mbps" in result
        assert "latency_ms" in result
        assert "timestamp" in result
        assert result["wifi_band"] is None  # ethernet
        assert result["avg_signal"] is None

    @pytest.mark.asyncio
    async def test_result_saved_to_json(self, tmp_path):
        """Result is persisted to disk."""
        fake_resp = MagicMock()
        fake_resp.content = b"\x00" * 10_000_000

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=fake_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        result_path = tmp_path / "speedtest.json"

        with patch("pi_decoder.network.SPEEDTEST_RESULT_PATH", result_path), \
             patch("pi_decoder.network.get_network_info_sync", return_value={"connection_type": "ethernet"}), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await run_speed_test()

        assert result_path.exists()
        saved = json.loads(result_path.read_text())
        assert "download_mbps" in saved

    @pytest.mark.asyncio
    async def test_concurrent_test_raises(self, tmp_path):
        """Second concurrent test raises RuntimeError."""
        fake_resp = MagicMock()
        fake_resp.content = b"\x00" * 10_000_000

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=fake_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        result_path = tmp_path / "speedtest.json"

        # Acquire the lock to simulate an in-progress test
        async with network._speed_test_lock:
            with patch("pi_decoder.network.SPEEDTEST_RESULT_PATH", result_path):
                with pytest.raises(RuntimeError, match="already in progress"):
                    await run_speed_test()

    @pytest.mark.asyncio
    async def test_network_error_propagates(self, tmp_path):
        """Network errors propagate to caller."""
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("no internet"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        result_path = tmp_path / "speedtest.json"

        with patch("pi_decoder.network.SPEEDTEST_RESULT_PATH", result_path), \
             patch("pi_decoder.network.get_network_info_sync", return_value={"connection_type": "wifi"}), \
             patch("subprocess.run", return_value=_make_sync_result("")), \
             patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.ConnectError):
                await run_speed_test()


class TestLoadSpeedTestResult:

    def test_returns_none_when_missing(self, tmp_path):
        with patch("pi_decoder.network.SPEEDTEST_RESULT_PATH", tmp_path / "missing.json"):
            assert load_speed_test_result() is None

    def test_returns_data_when_exists(self, tmp_path):
        result_path = tmp_path / "speedtest.json"
        data = {"download_mbps": 50.0, "latency_ms": 10.0, "timestamp": "2025-01-15T10:30:00"}
        result_path.write_text(json.dumps(data))
        with patch("pi_decoder.network.SPEEDTEST_RESULT_PATH", result_path):
            result = load_speed_test_result()
        assert result["download_mbps"] == 50.0

    def test_returns_none_on_invalid_json(self, tmp_path):
        result_path = tmp_path / "speedtest.json"
        result_path.write_text("not json {{{")
        with patch("pi_decoder.network.SPEEDTEST_RESULT_PATH", result_path):
            assert load_speed_test_result() is None
