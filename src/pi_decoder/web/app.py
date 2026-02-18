"""FastAPI web application — REST API, WebSocket, and web UI."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path

import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from pi_decoder.config import Config, save_config, load_config, to_dict_safe, validate_config
from pi_decoder.mpv_manager import MpvManager
from pi_decoder.overlay import OverlayUpdater, format_overlay, format_countdown
from pi_decoder.pco_client import PCOClient

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _cidr_to_subnet_mask(cidr_address: str) -> str:
    """Convert a CIDR address like '192.168.1.100/24' to its subnet mask '255.255.255.0'.

    Returns '255.255.255.0' as default if input has no prefix or is invalid.
    """
    try:
        if "/" in cidr_address:
            prefix = int(cidr_address.split("/")[1])
            bits = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
            return f"{(bits >> 24) & 0xFF}.{(bits >> 16) & 0xFF}.{(bits >> 8) & 0xFF}.{bits & 0xFF}"
    except (ValueError, IndexError):
        pass
    return "255.255.255.0"


_TEMPLATES.env.filters["subnet_mask"] = _cidr_to_subnet_mask


def _system_info() -> dict:
    """Gather CPU, memory, temperature, uptime."""
    try:
        cpu = psutil.cpu_percent(interval=0)
        mem = psutil.virtual_memory()
        temp = 0.0
        try:
            result = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True, text=True, timeout=3,
            )
            temp = float(result.stdout.strip().replace("temp=", "").replace("'C", ""))
        except Exception:
            # try thermal_zone on Linux
            try:
                for tz in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
                    temp = int(tz.read_text().strip()) / 1000.0
                    break
            except Exception:
                pass

        boot = psutil.boot_time()
        uptime_s = int(datetime.now().timestamp() - boot)
        days, rem = divmod(uptime_s, 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        if days:
            uptime_str = f"{days}d {hours}h"
        else:
            uptime_str = f"{hours}h {mins}m"

        return {
            "cpu_percent": round(cpu, 1),
            "memory_percent": round(mem.percent, 1),
            "memory_used_mb": mem.used // (1024 * 1024),
            "memory_total_mb": mem.total // (1024 * 1024),
            "temperature": round(temp, 1),
            "uptime": uptime_str,
            "uptime_seconds": uptime_s,
        }
    except Exception:
        return {
            "cpu_percent": 0, "memory_percent": 0,
            "memory_used_mb": 0, "memory_total_mb": 0,
            "temperature": 0, "uptime": "unknown", "uptime_seconds": 0,
        }


# Known captive portal check domains
_CAPTIVE_DOMAINS = {
    "connectivitycheck.gstatic.com",
    "clients3.google.com",
    "captive.apple.com",
    "www.apple.com",
    "msftconnecttest.com",
    "www.msftconnecttest.com",
    "detectportal.firefox.com",
    "nmcheck.gnome.org",
}


class CaptivePortalMiddleware(BaseHTTPMiddleware):
    """Redirect captive portal checks to the web UI when in hotspot mode."""

    def __init__(self, app, config: Config):
        super().__init__(app)
        self._config = config
        self._cached_info: dict | None = None
        self._cache_time: float = 0.0

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        # Only redirect if in hotspot mode and host is a captive portal domain
        if host in _CAPTIVE_DOMAINS:
            try:
                now = time.monotonic()
                # Cache hotspot status for 10s to avoid subprocess on every request
                if self._cached_info is None or (now - self._cache_time) > 10:
                    from pi_decoder.network import get_network_info_sync
                    self._cached_info = await asyncio.to_thread(get_network_info_sync)
                    self._cache_time = now
                if self._cached_info.get("hotspot_active"):
                    return RedirectResponse(
                        url=f"http://{self._cached_info.get('ip', '10.42.0.1')}/",
                    )
            except Exception:
                pass
        return await call_next(request)



def _build_overlay_info(config: Config, overlay: OverlayUpdater | None, pco: PCOClient | None = None) -> dict:
    """Build overlay status dict for API/WebSocket responses."""
    info: dict = {
        "enabled": config.overlay.enabled,
        "credentials_set": bool(config.pco.app_id),
        "timer_mode": config.overlay.timer_mode,
    }
    if overlay and overlay.running:
        st = overlay.last_status
        info["is_live"] = st.is_live
        info["finished"] = st.finished
        info["plan_title"] = st.plan_title
        info["item_title"] = st.item_title
        info["countdown"] = ""
        if st.service_end_time:
            remaining = (st.service_end_time - datetime.now(timezone.utc)).total_seconds()
            info["countdown"] = format_countdown(remaining)
        info["message"] = st.message
    if pco and pco.credential_error:
        info["credential_error"] = pco.credential_error
    return info


def create_app(
    mpv: MpvManager,
    pco: PCOClient | None,
    overlay: OverlayUpdater | None,
    config: Config,
    config_path: str = "/etc/pi-decoder/config.toml",
) -> FastAPI:
    app = FastAPI(title="Pi-Decoder", docs_url=None, redoc_url=None)

    # Captive portal middleware (redirects phone connectivity checks to web UI)
    app.add_middleware(CaptivePortalMiddleware, config=config)

    # static files
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # ── pages ────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return _TEMPLATES.TemplateResponse("index.html", {
            "request": request,
            "config": config,
        })

    # ── REST API ─────────────────────────────────────────────────────

    @app.get("/api/status")
    async def api_status():
        mpv_status = await mpv.get_status()
        overlay_info = _build_overlay_info(config, overlay, pco)
        network_info = {}
        try:
            from pi_decoder.network import get_network_info_sync
            network_info = get_network_info_sync()
        except Exception:
            pass

        return {
            "name": config.general.name,
            "mpv": mpv_status,
            "overlay": overlay_info,
            "system": _system_info(),
            "network": network_info,
        }

    @app.post("/api/config/general")
    async def api_config_general(request: Request):
        data = await request.json()
        if "name" in data:
            config.general.name = str(data["name"]).strip() or "Pi-Decoder"
        validate_config(config)
        save_config(config, config_path)
        log.info("Config updated: general name changed to %s", config.general.name)
        # Sync system hostname with decoder name
        from pi_decoder.hostname import set_hostname
        hostname = await set_hostname(config.general.name)
        return {"ok": True, "hostname": hostname}

    @app.post("/api/config/stream")
    async def api_config_stream(request: Request):
        data = await request.json()
        config.stream.url = data.get("url", config.stream.url)
        config.stream.network_caching = int(data.get("network_caching", config.stream.network_caching))
        validate_config(config)
        save_config(config, config_path)
        log.info("Config updated: stream URL changed to %s", config.stream.url[:50])
        # Reset retry backoff when URL changes so it tries immediately
        mpv.reset_stream_retry()
        return {"ok": True}

    @app.post("/api/config/overlay")
    async def api_config_overlay(request: Request):
        data = await request.json()
        config.overlay.enabled = data.get("enabled", config.overlay.enabled)
        config.overlay.position = data.get("position", config.overlay.position)
        config.overlay.font_size = int(data.get("font_size", config.overlay.font_size))
        config.overlay.font_size_title = int(data.get("font_size_title", config.overlay.font_size_title))
        config.overlay.font_size_info = int(data.get("font_size_info", config.overlay.font_size_info))
        config.overlay.transparency = float(data.get("transparency", config.overlay.transparency))
        config.overlay.timer_mode = data.get("timer_mode", config.overlay.timer_mode)
        config.overlay.show_description = data.get("show_description", config.overlay.show_description)
        config.overlay.show_service_end = data.get("show_service_end", config.overlay.show_service_end)
        config.overlay.timezone = data.get("timezone", config.overlay.timezone)
        validate_config(config)
        save_config(config, config_path)
        log.info("Config updated: overlay settings changed")
        return {"ok": True}

    @app.post("/api/config/pco")
    async def api_config_pco(request: Request):
        data = await request.json()
        if data.get("app_id"):
            config.pco.app_id = data["app_id"]
        if data.get("secret"):
            config.pco.secret = data["secret"]
        if data.get("service_type_id"):
            config.pco.service_type_id = data["service_type_id"]
        if "folder_id" in data:
            config.pco.folder_id = str(data["folder_id"])
        if "search_mode" in data:
            mode = str(data["search_mode"])
            if mode in ("service_type", "folder"):
                config.pco.search_mode = mode
        if "poll_interval" in data:
            config.pco.poll_interval = max(2, min(60, int(data["poll_interval"])))
        validate_config(config)
        save_config(config, config_path)
        # update PCO client credentials
        if pco:
            pco.update_credentials(
                config.pco.app_id, config.pco.secret,
                config.pco.service_type_id,
                folder_id=config.pco.folder_id,
                search_mode=config.pco.search_mode,
            )
        log.info("Config updated: PCO credentials updated")
        return {"ok": True}

    @app.post("/api/test-pco")
    async def api_test_pco(request: Request):
        data = await request.json()
        app_id = data.get("app_id", "")
        secret = data.get("secret", "")
        if not app_id or not secret:
            return JSONResponse({"success": False, "error": "Missing credentials"}, 400)
        from pi_decoder.pco_client import PCOClient as _PCO
        tmp_cfg = Config()
        tmp_cfg.pco.app_id = app_id
        tmp_cfg.pco.secret = secret
        tmp_cfg.pco.service_type_id = data.get("service_type_id", "")
        tmp_client = _PCO(tmp_cfg)
        try:
            result = await tmp_client.test_connection()
            return result
        finally:
            await tmp_client.close()

    @app.get("/api/service-types")
    async def api_service_types():
        if not pco:
            return JSONResponse({"ok": False, "error": "PCO not configured"}, 400)
        types = await pco.get_service_types()
        return {"service_types": types}

    @app.get("/api/health")
    async def api_health():
        return {"status": "ok"}

    @app.get("/api/screenshot")
    async def api_screenshot():
        data = await mpv.take_screenshot()
        if data:
            return Response(content=data, media_type="image/jpeg")
        return JSONResponse({"ok": False, "error": "Screenshot failed"}, 500)

    ALLOWED_LOG_SERVICES = {"pi-decoder"}

    @app.get("/api/logs")
    async def api_logs(service: str = "pi-decoder", lines: int = 50):
        if service not in ALLOWED_LOG_SERVICES:
            return JSONResponse({"error": "Invalid service"}, status_code=400)
        lines = min(lines, 1000)
        try:
            result = subprocess.run(
                ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=5,
            )
            return {"service": service, "logs": result.stdout}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Log fetch failed: {e}"}, 500)

    @app.post("/api/stop/video")
    async def api_stop_video():
        await mpv.stop_stream()
        return {"ok": True}

    @app.post("/api/restart/video")
    async def api_restart_video():
        asyncio.create_task(mpv.restart())
        return {"ok": True}

    @app.post("/api/restart/overlay")
    async def api_restart_overlay():
        if overlay:
            await overlay.stop()
            overlay.start_task()
        return {"ok": True}

    @app.post("/api/restart/all")
    async def api_restart_all():
        if overlay:
            await overlay.stop()
        asyncio.create_task(mpv.restart())
        if overlay:
            # restart overlay after mpv is back
            async def _restart_overlay():
                await asyncio.sleep(3)
                overlay.start_task()
            asyncio.create_task(_restart_overlay())
        return {"ok": True}

    @app.get("/api/version")
    async def api_version():
        try:
            ver = pkg_version("pi-decoder")
        except Exception:
            ver = "unknown"
        return {"version": ver}

    @app.post("/api/update")
    async def api_update(file: UploadFile = File(...)):
        filename = file.filename or ""
        if not (filename.endswith(".whl") or filename.endswith(".tar.gz")):
            return JSONResponse(
                {"ok": False, "error": "Only .whl or .tar.gz files accepted"},
                status_code=400,
            )

        # Read and check size
        content = await file.read()
        if len(content) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                {"ok": False, "error": "File too large (max 10MB)"},
                status_code=400,
            )

        # Save to temp file (secure)
        import tempfile
        suffix = ".whl" if filename.endswith(".whl") else ".tar.gz"
        fd, tmp_name = tempfile.mkstemp(suffix=suffix, prefix="pi-decoder-update-")
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(content)
            os.close(fd)

            # Install with venv pip (no --break-system-packages needed)
            venv_pip = Path("/opt/pi-decoder/venv/bin/pip")
            pip_cmd = [str(venv_pip), "install", str(tmp_path)] if venv_pip.exists() else \
                      [sys.executable, "-m", "pip", "install", "--break-system-packages", str(tmp_path)]
            result = subprocess.run(
                pip_cmd,
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return JSONResponse(
                    {"ok": False, "error": result.stderr or result.stdout},
                    status_code=500,
                )
        except subprocess.TimeoutExpired:
            return JSONResponse(
                {"ok": False, "error": "Install timed out"},
                status_code=500,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        # Read new version
        try:
            new_ver = pkg_version("pi-decoder")
        except Exception:
            new_ver = "unknown"

        # Schedule service restart after 2s so response reaches client
        async def _restart_service():
            await asyncio.sleep(2)
            subprocess.Popen(["sudo", "systemctl", "restart", "pi-decoder"])

        asyncio.create_task(_restart_service())

        return {"ok": True, "version": new_ver, "message": f"Updated to {new_ver}, restarting..."}

    @app.post("/api/reboot")
    async def api_reboot():
        subprocess.Popen(["sudo", "reboot"])
        return {"ok": True, "message": "System rebooting"}

    @app.post("/api/shutdown")
    async def api_shutdown():
        subprocess.Popen(["sudo", "poweroff"])
        return {"ok": True, "message": "System shutting down"}

    # ── Config backup/restore ─────────────────────────────────────

    @app.get("/api/config/export")
    async def api_config_export():
        data = to_dict_safe(config)
        buf = io.BytesIO()
        import tomli_w
        tomli_w.dump(data, buf)
        hostname = socket.gethostname()
        return Response(
            content=buf.getvalue(),
            media_type="application/toml",
            headers={"Content-Disposition": f'attachment; filename="{hostname}-config.toml"'},
        )

    @app.post("/api/config/import")
    async def api_config_import(file: UploadFile = File(...)):
        filename = file.filename or ""
        if not filename.endswith(".toml"):
            return JSONResponse({"ok": False, "error": "Only .toml files accepted"}, 400)
        content = await file.read()
        if len(content) > 64 * 1024:
            return JSONResponse({"ok": False, "error": "File too large (max 64KB)"}, 400)
        try:
            if sys.version_info >= (3, 11):
                import tomllib
            else:
                try:
                    import tomllib
                except ModuleNotFoundError:
                    import tomli as tomllib  # type: ignore[no-redef]
            raw = tomllib.loads(content.decode("utf-8"))
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Invalid TOML: {e}"}, 400)
        # Merge imported config (skip secrets — they stay as-is)
        from pi_decoder.config import _apply_dict
        if "general" in raw:
            _apply_dict(config.general, raw["general"])
        if "stream" in raw:
            _apply_dict(config.stream, raw["stream"])
        if "overlay" in raw:
            _apply_dict(config.overlay, raw["overlay"])
        if "pco" in raw:
            # Preserve existing secret if not in import
            pco_data = raw["pco"]
            if "secret" not in pco_data:
                pco_data["secret"] = config.pco.secret
            _apply_dict(config.pco, pco_data)
        if "web" in raw:
            _apply_dict(config.web, raw["web"])
        if "network" in raw:
            _apply_dict(config.network, raw["network"])
        validate_config(config)
        save_config(config, config_path)
        return {"ok": True, "message": "Config imported successfully"}

    # ── Network management ─────────────────────────────────────────

    @app.get("/api/network/status")
    async def api_network_status():
        from pi_decoder.network import get_network_info_sync
        try:
            return await asyncio.to_thread(get_network_info_sync)
        except Exception as e:
            return JSONResponse({"error": f"Network status unavailable: {e}"}, 500)

    @app.get("/api/network/wifi-scan")
    async def api_network_wifi_scan():
        from pi_decoder.network import scan_wifi
        try:
            networks = await scan_wifi()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"WiFi scan failed: {e}"}, 500)
        return {"networks": networks}

    @app.post("/api/network/wifi-connect")
    async def api_network_wifi_connect(request: Request):
        from pi_decoder.network import connect_wifi
        data = await request.json()
        ssid = data.get("ssid", "").strip()
        password = data.get("password", "")
        if not ssid or len(ssid.encode()) > 32:
            return JSONResponse({"ok": False, "error": "SSID must be 1-32 bytes"}, 400)
        if password and (len(password) < 8 or len(password) > 63):
            return JSONResponse({"ok": False, "error": "Password must be 8-63 characters"}, 400)
        try:
            result = await connect_wifi(ssid, password)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Connection failed: {e}"}, 500)
        # Reset stream retry on network change
        mpv.reset_stream_retry()
        return {"ok": True, "message": result}

    @app.post("/api/network/hotspot/start")
    async def api_network_hotspot_start():
        from pi_decoder.network import start_hotspot, get_network_info_sync
        # Hotspot guard: reject if Ethernet or WiFi is active
        try:
            net = await asyncio.to_thread(get_network_info_sync)
            if net.get("connection_type") in ("ethernet", "wifi"):
                return JSONResponse({"ok": False,
                    "error": "Cannot start hotspot while connected via "
                             + net["connection_type"] + ". Disconnect first."}, 400)
        except Exception:
            pass
        try:
            await start_hotspot(config.network.hotspot_ssid, config.network.hotspot_password)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Hotspot start failed: {e}"}, 500)
        return {"ok": True}

    @app.post("/api/network/hotspot/stop")
    async def api_network_hotspot_stop():
        from pi_decoder.network import stop_hotspot
        try:
            await stop_hotspot()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Hotspot stop failed: {e}"}, 500)
        mpv.reset_stream_retry()
        return {"ok": True}

    @app.get("/api/network/wifi/saved")
    async def api_network_wifi_saved():
        from pi_decoder.network import get_saved_networks
        try:
            return {"networks": await get_saved_networks()}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Could not list networks: {e}"}, 500)

    @app.post("/api/network/wifi/forget")
    async def api_network_wifi_forget(request: Request):
        from pi_decoder.network import forget_network
        data = await request.json()
        name = data.get("name", "")
        if not name:
            return JSONResponse({"ok": False, "error": "Network name required"}, 400)
        try:
            await forget_network(name)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Could not forget network: {e}"}, 500)
        return {"ok": True}

    @app.post("/api/config/network")
    async def api_config_network(request: Request):
        data = await request.json()
        if "hotspot_ssid" in data:
            config.network.hotspot_ssid = str(data["hotspot_ssid"])
        if "hotspot_password" in data:
            config.network.hotspot_password = str(data["hotspot_password"])
        if "ethernet_timeout" in data:
            config.network.ethernet_timeout = int(data["ethernet_timeout"])
        if "wifi_timeout" in data:
            config.network.wifi_timeout = int(data["wifi_timeout"])
        # Static IP fields (eth_ and wifi_ prefixes)
        for prefix in ("eth", "wifi"):
            for suffix in ("ip_mode", "ip_address", "gateway", "dns"):
                key = f"{prefix}_{suffix}"
                if key in data:
                    setattr(config.network, key, str(data[key]))
        validate_config(config)
        save_config(config, config_path)
        log.info("Config updated: network settings changed")
        return {"ok": True}

    @app.post("/api/network/apply-ip")
    async def api_network_apply_ip(request: Request):
        from pi_decoder.network import apply_static_ip
        data = await request.json()
        iface = data.get("interface", "")
        if iface not in ("ethernet", "wifi"):
            return JSONResponse({"ok": False, "error": "Invalid interface (ethernet or wifi)"}, 400)
        prefix = "eth" if iface == "ethernet" else "wifi"
        mode = getattr(config.network, f"{prefix}_ip_mode")
        address = getattr(config.network, f"{prefix}_ip_address")
        gateway = getattr(config.network, f"{prefix}_gateway")
        dns = getattr(config.network, f"{prefix}_dns")
        try:
            msg = await apply_static_ip(iface, mode, address, gateway, dns)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, 500)
        mpv.reset_stream_retry()
        return {"ok": True, "message": msg}

    @app.get("/api/network/speedtest")
    async def api_network_speedtest_get():
        from pi_decoder.network import load_speed_test_result
        result = load_speed_test_result()
        return {"ok": True, "result": result}

    @app.post("/api/network/speedtest")
    async def api_network_speedtest_post():
        from pi_decoder.network import run_speed_test
        try:
            result = await run_speed_test()
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return {"ok": True, **result}

    # ── CEC TV control ─────────────────────────────────────────────

    @app.post("/api/cec/on")
    async def api_cec_on():
        try:
            from pi_decoder import cec
            await cec.power_on()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC power on failed: {e}"}, 500)
        return {"ok": True}

    @app.post("/api/cec/standby")
    async def api_cec_standby():
        try:
            from pi_decoder import cec
            await cec.standby()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC standby failed: {e}"}, 500)
        return {"ok": True}

    @app.get("/api/cec/power-status")
    async def api_cec_power_status():
        try:
            from pi_decoder import cec
            status = await cec.get_power_status()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC status check failed: {e}"}, 500)
        return {"ok": True, "status": status}

    @app.post("/api/cec/active-source")
    async def api_cec_active_source():
        try:
            from pi_decoder import cec
            await cec.active_source()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC active source failed: {e}"}, 500)
        return {"ok": True}

    @app.post("/api/cec/input")
    async def api_cec_input(request: Request):
        data = await request.json()
        try:
            port = int(data.get("port", 1))
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "Invalid port number"}, status_code=400)
        try:
            from pi_decoder import cec
            await cec.set_input(port)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC set input failed: {e}"}, 500)
        return {"ok": True}

    @app.post("/api/cec/volume-up")
    async def api_cec_volume_up():
        try:
            from pi_decoder import cec
            await cec.volume_up()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC volume up failed: {e}"}, 500)
        return {"ok": True}

    @app.post("/api/cec/volume-down")
    async def api_cec_volume_down():
        try:
            from pi_decoder import cec
            await cec.volume_down()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC volume down failed: {e}"}, 500)
        return {"ok": True}

    @app.post("/api/cec/mute")
    async def api_cec_mute():
        try:
            from pi_decoder import cec
            await cec.mute()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"CEC mute failed: {e}"}, 500)
        return {"ok": True}

    # ── WebSocket: status ────────────────────────────────────────────

    @app.websocket("/ws/status")
    async def ws_status(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                mpv_status = await mpv.get_status()
                overlay_info = _build_overlay_info(config, overlay, pco)
                network_info = {}
                try:
                    from pi_decoder.network import get_network_info_sync
                    network_info = await asyncio.to_thread(get_network_info_sync)
                except Exception:
                    pass
                # Include hotspot config so UI banner stays in sync
                # Strip sensitive fields from broadcast
                network_info.pop("hotspot_password", None)
                network_info["hotspot_ssid"] = config.network.hotspot_ssid

                # CEC power status (best effort, don't block on failure)
                cec_status = "unknown"
                try:
                    from pi_decoder import cec
                    cec_status = await asyncio.wait_for(cec.get_power_status(), timeout=3)
                except Exception:
                    pass

                await ws.send_json({
                    "name": config.general.name,
                    "hostname": socket.gethostname(),
                    "mpv": mpv_status,
                    "overlay": overlay_info,
                    "system": _system_info(),
                    "network": network_info,
                    "cec": {"power": cec_status},
                })
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("Status WebSocket error", exc_info=True)

    # ── WebSocket: preview ───────────────────────────────────────────

    @app.websocket("/ws/preview")
    async def ws_preview(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                data = await mpv.take_screenshot()
                if data:
                    await ws.send_bytes(data)
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("Preview WebSocket error", exc_info=True)

    return app
