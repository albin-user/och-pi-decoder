#!/usr/bin/env python3
"""Local dev server for previewing the Pi-Decoder web UI on macOS.

Mocks hardware-dependent components (mpv, CEC, nmcli, journalctl) so the
real FastAPI app can serve real HTML/CSS/JS on localhost:8080.

Usage:
    .venv/bin/python dev_server.py
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from pi_decoder.config import Config
from pi_decoder.web.app import create_app

DEV_CONFIG_PATH = "/tmp/dev-decoder-config.toml"


def make_config() -> Config:
    """Build a Config with demo defaults."""
    cfg = Config()
    cfg.general.name = "Dev-Decoder"
    cfg.stream.url = "rtmp://192.168.1.50:1935/live/stream"
    cfg.overlay.enabled = True
    cfg.overlay.position = "bottom-right"
    cfg.network.hotspot_ssid = "Dev-Decoder"
    cfg.network.hotspot_password = "devpassword"
    return cfg


def make_mock_mpv() -> MagicMock:
    mpv = MagicMock()
    mpv.get_status = AsyncMock(return_value={
        "alive": True,
        "playing": True,
        "idle": False,
        "stream_url": "rtmp://192.168.1.50:1935/live/stream",
    })
    mpv.restart = AsyncMock()
    mpv.stop_stream = AsyncMock()
    mpv.reset_stream_retry = MagicMock()
    mpv.take_screenshot = AsyncMock(return_value=None)
    return mpv


def make_mock_pco() -> MagicMock:
    pco = MagicMock()
    pco.update_credentials = MagicMock()
    pco.get_service_types = AsyncMock(return_value=[
        {"id": "1", "name": "Sunday Service"},
        {"id": "2", "name": "Wednesday Night"},
    ])
    pco.test_connection = AsyncMock(return_value={"success": True, "service_types": []})
    pco.close = AsyncMock()
    return pco


def make_mock_overlay() -> MagicMock:
    overlay = MagicMock()
    overlay.running = True
    overlay.last_status = MagicMock(
        is_live=True,
        finished=False,
        plan_title="Sunday Morning Service",
        item_title="Worship Set",
        service_end_time=datetime.now(timezone.utc) + timedelta(minutes=45),
        message="",
    )
    overlay.stop = AsyncMock()
    overlay.start_task = MagicMock()
    return overlay


FAKE_NETWORK_INFO = {
    "ip": "192.168.1.100",
    "ssid": "DemoWiFi",
    "signal": 72,
    "mode": "wifi",
    "hotspot_active": False,
    "interface": "en0",
    "gateway": "192.168.1.1",
    "dns": "192.168.1.1",
    "mac": "AA:BB:CC:DD:EE:FF",
}


def fake_subprocess_run(cmd, **kwargs):
    """Handle subprocess.run calls that would fail on macOS."""
    prog = cmd[0] if cmd else ""
    if prog == "journalctl":
        result = MagicMock()
        result.stdout = (
            "-- Logs begin at Thu 2025-01-01 00:00:00 UTC --\n"
            "Jan 01 12:00:00 dev-decoder pi-decoder[1234]: Stream connected\n"
            "Jan 01 12:00:02 dev-decoder pi-decoder[1234]: Overlay started\n"
            "Jan 01 12:00:05 dev-decoder pi-decoder[1234]: WebSocket client connected\n"
        )
        result.returncode = 0
        return result
    if prog == "vcgencmd":
        result = MagicMock()
        result.stdout = "temp=42.0'C"
        result.returncode = 0
        return result
    # Fall through to real subprocess for anything else
    return original_subprocess_run(cmd, **kwargs)


original_subprocess_run = subprocess.run
original_subprocess_popen = subprocess.Popen


def fake_subprocess_popen(cmd, **kwargs):
    """Intercept subprocess.Popen for reboot/poweroff/restart commands."""
    prog = cmd[-1] if cmd else ""
    if prog in ("reboot", "poweroff") or (len(cmd) >= 3 and cmd[1] == "systemctl" and cmd[2] == "restart"):
        print(f"  [dev] Intercepted Popen: {' '.join(cmd)}")
        mock = MagicMock()
        mock.returncode = 0
        mock.pid = 9999
        return mock
    return original_subprocess_popen(cmd, **kwargs)


def main():
    import uvicorn

    config = make_config()
    app = create_app(
        make_mock_mpv(),
        make_mock_pco(),
        make_mock_overlay(),
        config,
        DEV_CONFIG_PATH,
    )

    patches = [
        patch("pi_decoder.network.get_network_info_sync", return_value=FAKE_NETWORK_INFO),
        patch("pi_decoder.network.scan_wifi", new_callable=AsyncMock, return_value=[
            {"ssid": "DemoWiFi", "signal": 72, "security": "WPA2"},
            {"ssid": "Neighbor5G", "signal": 45, "security": "WPA2"},
        ]),
        patch("pi_decoder.network.connect_wifi", new_callable=AsyncMock, return_value="Connected"),
        patch("pi_decoder.network.start_hotspot", new_callable=AsyncMock),
        patch("pi_decoder.network.stop_hotspot", new_callable=AsyncMock),
        patch("pi_decoder.network.get_saved_networks", new_callable=AsyncMock, return_value=[
            {"name": "DemoWiFi", "uuid": "abc-123"},
        ]),
        patch("pi_decoder.network.forget_network", new_callable=AsyncMock),
        patch("pi_decoder.network.run_speed_test", new_callable=AsyncMock, return_value={
            "download_mbps": 47.23, "latency_ms": 12.3,
            "timestamp": "2025-01-15T10:30:00",
            "wifi_band": "5 GHz", "avg_signal": 72, "interface_type": "USB adapter",
        }),
        patch("pi_decoder.network.load_speed_test_result", return_value={
            "download_mbps": 47.23, "latency_ms": 12.3,
            "timestamp": "2025-01-15T10:30:00",
            "wifi_band": "5 GHz", "avg_signal": 72, "interface_type": "USB adapter",
        }),
        patch("pi_decoder.hostname.set_hostname", new_callable=AsyncMock, return_value="dev-decoder"),
        patch("pi_decoder.cec.power_on", new_callable=AsyncMock),
        patch("pi_decoder.cec.standby", new_callable=AsyncMock),
        patch("pi_decoder.cec.get_power_status", new_callable=AsyncMock, return_value="on"),
        patch("pi_decoder.cec.active_source", new_callable=AsyncMock),
        patch("pi_decoder.cec.set_input", new_callable=AsyncMock),
        patch("pi_decoder.cec.volume_up", new_callable=AsyncMock),
        patch("pi_decoder.cec.volume_down", new_callable=AsyncMock),
        patch("pi_decoder.cec.mute", new_callable=AsyncMock),
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch("subprocess.Popen", side_effect=fake_subprocess_popen),
    ]

    for p in patches:
        p.start()

    print(f"\n  Pi-Decoder dev server")
    print(f"  http://localhost:8080\n")
    print(f"  Config saves to: {DEV_CONFIG_PATH}")
    print(f"  All hardware calls are mocked.\n")

    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="info")


if __name__ == "__main__":
    main()
