"""Network management via nmcli (NetworkManager)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SPEEDTEST_RESULT_PATH = Path("/etc/pi-decoder/speedtest.json")

_speed_test_lock = asyncio.Lock()

_TIMEOUT = 15  # seconds


async def _run_nmcli(*args: str) -> str:
    """Run an nmcli command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "nmcli", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT,
        )
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"nmcli failed (rc={proc.returncode}): {err}")
        return stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError("nmcli timed out")


def get_ip_for_interface(iface: str = "") -> str:
    """Sync IP lookup via nmcli. Fast (<100ms). Returns IP or empty string."""
    try:
        if iface:
            result = subprocess.run(
                ["nmcli", "-g", "IP4.ADDRESS", "device", "show", iface],
                capture_output=True, text=True, timeout=3,
            )
        else:
            result = subprocess.run(
                ["nmcli", "-g", "IP4.ADDRESS", "device", "show"],
                capture_output=True, text=True, timeout=3,
            )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("127."):
                return line.split("/")[0]
    except Exception as e:
        log.warning("Failed to get IP for %s: %s", iface or "default", e)
    return ""


def get_network_info_sync() -> dict:
    """Sync network info for idle screen overlay. Returns dict with
    connection_type, ip, ssid, hotspot_active, signal."""
    info: dict = {
        "connection_type": "none",
        "ip": "",
        "ssid": "",
        "hotspot_active": False,
        "signal": 0,
    }
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"],
            capture_output=True, text=True, timeout=3,
        )
        # Two-pass: collect connected devices, then prioritize ethernet > wifi > hotspot
        eth_info = None
        wifi_info = None
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            dev, dtype, state, conn = parts[0], parts[1], parts[2], parts[3]
            if state != "connected":
                continue
            if dtype == "ethernet" and not eth_info:
                eth_info = (dev, conn)
            elif dtype == "wifi" and not wifi_info:
                wifi_info = (dev, conn)

        if eth_info:
            info["connection_type"] = "ethernet"
            info["ip"] = get_ip_for_interface(eth_info[0])
            # Still track hotspot if wifi is also connected as hotspot
            if wifi_info:
                conn = wifi_info[1]
                if conn.lower() == "hotspot" or "hotspot" in conn.lower():
                    info["hotspot_active"] = True
        elif wifi_info:
            dev, conn = wifi_info
            if conn.lower() == "hotspot" or "hotspot" in conn.lower():
                info["connection_type"] = "hotspot"
                info["hotspot_active"] = True
                info["ip"] = get_ip_for_interface(dev)
                info["ssid"] = conn
            else:
                info["connection_type"] = "wifi"
                info["ssid"] = conn
                info["ip"] = get_ip_for_interface(dev)

        # Get WiFi signal strength if connected via WiFi
        if info["connection_type"] == "wifi":
            try:
                sig_result = subprocess.run(
                    ["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "device", "wifi"],
                    capture_output=True, text=True, timeout=3,
                )
                for line in sig_result.stdout.strip().splitlines():
                    if line.startswith("yes:"):
                        info["signal"] = int(line.split(":")[1])
                        break
            except Exception:
                pass

    except Exception:
        log.debug("get_network_info_sync failed", exc_info=True)

    # Fallback IP via socket if nmcli didn't find one
    if not info["ip"]:
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                info["ip"] = s.getsockname()[0]
            if info["connection_type"] == "none":
                info["connection_type"] = "unknown"
        except Exception:
            pass

    return info


async def get_network_status() -> dict:
    """Async network status."""
    return await asyncio.to_thread(get_network_info_sync)


async def scan_wifi() -> list[dict]:
    """Scan for WiFi networks. Returns deduplicated list sorted by signal."""
    try:
        # Force a rescan
        try:
            await _run_nmcli("device", "wifi", "rescan")
        except Exception:
            pass  # rescan may fail if already scanning
        await asyncio.sleep(2)

        output = await _run_nmcli(
            "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list",
        )
    except Exception:
        log.debug("WiFi scan failed", exc_info=True)
        return []

    seen: dict[str, dict] = {}
    for line in output.strip().splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        ssid = parts[0].strip()
        if not ssid or ssid == "--":
            continue
        signal = 0
        try:
            signal = int(parts[1])
        except ValueError:
            pass
        security = parts[2].strip()
        in_use = parts[3].strip() == "*"

        # Keep highest signal for each SSID
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = {
                "ssid": ssid,
                "signal": signal,
                "security": security,
                "in_use": in_use,
            }

    return sorted(seen.values(), key=lambda x: x["signal"], reverse=True)


async def connect_wifi(ssid: str, password: str) -> str:
    """Connect to a WiFi network. Stops hotspot first if active."""
    # Stop hotspot if it's running
    status = await get_network_status()
    if status.get("hotspot_active"):
        await stop_hotspot()
        await asyncio.sleep(2)

    try:
        output = await _run_nmcli(
            "device", "wifi", "connect", ssid, "password", password,
        )
        log.info("WiFi connect to %s: %s", ssid, output.strip())
        return output.strip()
    except RuntimeError as e:
        # Try with existing saved connection
        try:
            output = await _run_nmcli("connection", "up", ssid)
            return output.strip()
        except Exception:
            raise e


async def start_hotspot(ssid: str = "Decoder", password: str = "decodersetup") -> str:
    """Start WiFi hotspot with built-in DHCP."""
    try:
        # Delete existing hotspot connection if any
        try:
            await _run_nmcli("connection", "delete", "Hotspot")
        except Exception:
            pass

        output = await _run_nmcli(
            "device", "wifi", "hotspot",
            "ifname", "wlan0",
            "ssid", ssid,
            "password", password,
        )
        # Configure DNS to point to self for captive portal
        try:
            await _run_nmcli(
                "connection", "modify", "Hotspot",
                "ipv4.dns", "10.42.0.1",
            )
        except Exception:
            pass

        log.info("Hotspot started: SSID=%s", ssid)
        return output.strip()
    except Exception:
        log.exception("Failed to start hotspot")
        raise


async def stop_hotspot() -> str:
    """Stop the WiFi hotspot."""
    try:
        output = await _run_nmcli("connection", "down", "Hotspot")
        log.info("Hotspot stopped")
        return output.strip()
    except Exception:
        log.debug("Hotspot stop failed (may not be running)", exc_info=True)
        return "not running"


async def get_saved_networks() -> list[str]:
    """List saved WiFi connection names."""
    try:
        output = await _run_nmcli(
            "-t", "-f", "NAME,TYPE", "connection", "show",
        )
    except Exception:
        return []

    networks = []
    for line in output.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1].strip() == "802-11-wireless":
            name = parts[0].strip()
            if name.lower() != "hotspot":
                networks.append(name)
    return networks


async def forget_network(name: str) -> str:
    """Delete a saved WiFi connection."""
    output = await _run_nmcli("connection", "delete", name)
    log.info("Forgot network: %s", name)
    return output.strip()


# ── Static IP management ─────────────────────────────────────────


async def get_active_connection_name(interface_type: str) -> str:
    """Get the active NM connection name for 'ethernet' or 'wifi'.

    Skips connections named 'Hotspot'. Returns empty string if none found.
    """
    output = await _run_nmcli(
        "-t", "-f", "NAME,TYPE", "connection", "show", "--active",
    )
    target_type = "802-3-ethernet" if interface_type == "ethernet" else "802-11-wireless"
    for line in output.strip().splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        name, ctype = parts[0].strip(), parts[1].strip()
        if ctype == target_type and name.lower() != "hotspot":
            return name
    return ""


async def apply_static_ip(
    interface_type: str,
    mode: str,
    address: str = "",
    gateway: str = "",
    dns: str = "",
) -> str:
    """Apply static IP or revert to DHCP on the active connection.

    Args:
        interface_type: 'ethernet' or 'wifi'
        mode: 'manual' or 'auto'
        address: CIDR notation e.g. '192.168.1.100/24'
        gateway: e.g. '192.168.1.1'
        dns: comma-separated e.g. '8.8.8.8, 8.8.4.4'

    Returns status message.
    """
    conn = await get_active_connection_name(interface_type)
    if not conn:
        raise RuntimeError(f"No active {interface_type} connection")

    if mode == "manual":
        # Convert comma-separated DNS to space-separated for nmcli
        dns_nmcli = " ".join(d.strip() for d in dns.split(",") if d.strip()) if dns else ""
        # Fall back to gateway as DNS if no DNS specified
        if not dns_nmcli and gateway:
            dns_nmcli = gateway
        await _run_nmcli(
            "connection", "modify", conn,
            "ipv4.method", "manual",
            "ipv4.addresses", address,
            "ipv4.gateway", gateway or "",
            "ipv4.dns", dns_nmcli or "",
        )
    else:
        await _run_nmcli(
            "connection", "modify", conn,
            "ipv4.method", "auto",
            "ipv4.addresses", "",
            "ipv4.gateway", "",
            "ipv4.dns", "",
        )

    # Re-apply the connection to activate changes
    await _run_nmcli("connection", "up", conn)
    return f"IP {mode} applied to {conn}"


# ── Speed test ────────────────────────────────────────────────────


def load_speed_test_result() -> dict | None:
    """Load last speed test result from disk. Returns None if missing or invalid."""
    try:
        data = json.loads(SPEEDTEST_RESULT_PATH.read_text())
        return data
    except Exception:
        return None


def _get_wifi_metadata() -> dict:
    """Capture WiFi band, signal, and interface type from wlan0."""
    meta: dict = {"wifi_band": None, "avg_signal": None, "interface_type": None}
    try:
        # Signal strength from nmcli
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "device", "wifi"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.strip().splitlines():
            if line.startswith("yes:"):
                meta["avg_signal"] = int(line.split(":")[1])
                break
    except Exception:
        pass
    try:
        # Band from iw (frequency)
        result = subprocess.run(
            ["iw", "dev", "wlan0", "link"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            if "freq:" in line:
                freq = int(line.strip().split("freq:")[1].strip().split()[0])
                if freq < 3000:
                    meta["wifi_band"] = "2.4 GHz"
                elif freq < 5900:
                    meta["wifi_band"] = "5 GHz"
                else:
                    meta["wifi_band"] = "6 GHz"
                break
    except Exception:
        pass
    try:
        # Interface type: USB adapter or built-in
        real = os.path.realpath("/sys/class/net/wlan0/device")
        meta["interface_type"] = "USB adapter" if "usb" in real.lower() else "Built-in"
    except Exception:
        pass
    return meta


async def run_speed_test() -> dict:
    """Run a download speed test against Cloudflare's edge network.

    Returns dict with download_mbps, latency_ms, timestamp, and optional
    wifi_band, avg_signal, interface_type (null when on Ethernet).
    """
    if _speed_test_lock.locked():
        raise RuntimeError("Speed test already in progress")

    async with _speed_test_lock:
        import httpx

        # Check if on WiFi
        net_info = await asyncio.to_thread(get_network_info_sync)
        on_wifi = net_info.get("connection_type") == "wifi"

        # Capture WiFi metadata before download
        wifi_before: dict = {}
        if on_wifi:
            wifi_before = await asyncio.to_thread(_get_wifi_metadata)

        async with httpx.AsyncClient(timeout=30) as client:
            # Latency: 3x small request, take min RTT
            latencies = []
            for _ in range(3):
                start = time.monotonic()
                await client.get("https://speed.cloudflare.com/__down?bytes=0")
                latencies.append((time.monotonic() - start) * 1000)
            latency_ms = round(min(latencies), 1)

            # Download: 10 MB
            start = time.monotonic()
            resp = await client.get("https://speed.cloudflare.com/__down?bytes=10000000")
            elapsed = time.monotonic() - start
            _ = resp.content  # ensure fully read
            download_mbps = round((10_000_000 * 8) / (elapsed * 1_000_000), 2)

        # Capture WiFi metadata after download, average signal
        wifi_meta: dict = {"wifi_band": None, "avg_signal": None, "interface_type": None}
        if on_wifi:
            wifi_after = await asyncio.to_thread(_get_wifi_metadata)
            wifi_meta["wifi_band"] = wifi_before.get("wifi_band") or wifi_after.get("wifi_band")
            wifi_meta["interface_type"] = wifi_before.get("interface_type") or wifi_after.get("interface_type")
            sig_before = wifi_before.get("avg_signal")
            sig_after = wifi_after.get("avg_signal")
            if sig_before is not None and sig_after is not None:
                wifi_meta["avg_signal"] = round((sig_before + sig_after) / 2)
            else:
                wifi_meta["avg_signal"] = sig_before or sig_after

        result = {
            "download_mbps": download_mbps,
            "latency_ms": latency_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **wifi_meta,
        }

        # Persist to disk
        try:
            SPEEDTEST_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SPEEDTEST_RESULT_PATH.write_text(json.dumps(result))
        except Exception:
            log.warning("Could not save speed test result", exc_info=True)

        return result
