"""Configuration manager — TOML load/save/validate."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

import tomli_w

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/pi-decoder/config.toml")


# ── dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class StreamConfig:
    url: str = ""
    backup_url: str = ""  # failover URL — auto-switch after consecutive failures
    network_caching: int = 2000
    hwdec: str = "auto"
    max_resolution: str = "1080"
    presets: list = field(default_factory=list)  # [{label: str, url: str}, ...]


@dataclass
class OverlayConfig:
    enabled: bool = False
    position: str = "bottom-right"
    font_size: int = 96
    font_size_title: int = 38
    font_size_info: int = 32
    transparency: float = 0.7
    timer_mode: str = "service"
    show_description: bool = False
    show_service_end: bool = True
    timezone: str = "Europe/Copenhagen"


@dataclass
class PCOConfig:
    app_id: str = ""
    secret: str = ""
    service_type_id: str = ""  # used when search_mode = "service_type"
    folder_id: str = ""  # used when search_mode = "folder"
    search_mode: str = "service_type"  # "service_type" or "folder"
    poll_interval: int = 5


@dataclass
class WebConfig:
    port: int = 80


@dataclass
class GeneralConfig:
    name: str = "Pi-Decoder"


@dataclass
class NetworkConfig:
    hotspot_ssid: str = "Pi-Decoder"
    hotspot_password: str = "pidecodersetup"
    ethernet_timeout: int = 10
    wifi_timeout: int = 40
    eth_ip_mode: str = "auto"
    eth_ip_address: str = ""
    eth_gateway: str = ""
    eth_dns: str = ""
    wifi_ip_mode: str = "auto"
    wifi_ip_address: str = ""
    wifi_gateway: str = ""
    wifi_dns: str = ""


@dataclass
class DisplayConfig:
    hdmi_resolution: str = "1920x1080@60D"


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    pco: PCOConfig = field(default_factory=PCOConfig)
    web: WebConfig = field(default_factory=WebConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)


# ── helpers ──────────────────────────────────────────────────────────────────


def _apply_dict(dc: object, data: dict) -> None:
    """Overwrite dataclass fields from a dict, skipping unknown keys."""
    for f in fields(dc):  # type: ignore[arg-type]
        if f.name in data:
            val = data[f.name]
            # coerce basic types
            if f.type in ("int", int):
                val = int(val)
            elif f.type in ("float", float):
                val = float(val)
            elif f.type in ("bool", bool):
                if isinstance(val, str):
                    val = val.lower() in ("true", "1", "yes")
            elif f.type in ("str", str):
                val = str(val)
            elif f.type in ("list", list):
                if not isinstance(val, list):
                    val = list(val)
            setattr(dc, f.name, val)


def _section_to_dict(dc: object) -> dict:
    """Convert a dataclass section to a plain dict (one level)."""
    return {f.name: getattr(dc, f.name) for f in fields(dc)}  # type: ignore[arg-type]


# ── public API ───────────────────────────────────────────────────────────────


def _validate_static_ip(net: NetworkConfig, prefix: str) -> None:
    """Validate static IP fields for a given prefix (eth/wifi).

    If mode is 'manual', validate address/gateway/dns. Invalid values
    revert mode to 'auto' and clear the fields.
    """
    import ipaddress

    mode = getattr(net, f"{prefix}_ip_mode")
    if mode not in ("auto", "manual"):
        setattr(net, f"{prefix}_ip_mode", "auto")
        mode = "auto"

    if mode != "manual":
        return

    addr = getattr(net, f"{prefix}_ip_address").strip()
    gw = getattr(net, f"{prefix}_gateway").strip()
    dns = getattr(net, f"{prefix}_dns").strip()

    # Validate CIDR address
    try:
        if not addr:
            raise ValueError("empty address")
        ipaddress.IPv4Interface(addr)
    except (ValueError, ipaddress.AddressValueError):
        log.warning("Invalid %s static IP '%s', reverting to DHCP", prefix, addr)
        setattr(net, f"{prefix}_ip_mode", "auto")
        setattr(net, f"{prefix}_ip_address", "")
        setattr(net, f"{prefix}_gateway", "")
        setattr(net, f"{prefix}_dns", "")
        return

    # Validate gateway (optional but must be valid if set)
    if gw:
        try:
            ipaddress.IPv4Address(gw)
        except (ValueError, ipaddress.AddressValueError):
            log.warning("Invalid %s gateway '%s', clearing", prefix, gw)
            setattr(net, f"{prefix}_gateway", "")

    # Validate DNS (comma-separated, each must be valid)
    if dns:
        valid_dns = []
        for entry in dns.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                ipaddress.IPv4Address(entry)
                valid_dns.append(entry)
            except (ValueError, ipaddress.AddressValueError):
                log.warning("Invalid %s DNS entry '%s', skipping", prefix, entry)
        setattr(net, f"{prefix}_dns", ", ".join(valid_dns))


def validate_config(cfg: Config) -> None:
    """Clamp and validate all config values in place."""
    cfg.network.ethernet_timeout = max(1, min(cfg.network.ethernet_timeout, 120))
    cfg.network.wifi_timeout = max(1, min(cfg.network.wifi_timeout, 120))

    _allowed_protocols = ("rtmp://", "rtmps://", "srt://", "http://", "https://", "rtp://", "udp://")
    _url = cfg.stream.url
    if _url and not any(_url.startswith(p) for p in _allowed_protocols):
        log.warning("Invalid stream URL protocol: %s", _url)

    cfg.stream.network_caching = max(200, min(cfg.stream.network_caching, 30000))
    cfg.overlay.font_size = max(10, min(cfg.overlay.font_size, 200))
    cfg.overlay.font_size_title = max(10, min(cfg.overlay.font_size_title, 200))
    cfg.overlay.font_size_info = max(10, min(cfg.overlay.font_size_info, 200))
    cfg.overlay.transparency = max(0.0, min(cfg.overlay.transparency, 1.0))
    if cfg.overlay.position not in (
        "top-left",
        "top-right",
        "bottom-left",
        "bottom-right",
    ):
        cfg.overlay.position = "bottom-right"
    if cfg.overlay.timer_mode not in ("service", "item"):
        cfg.overlay.timer_mode = "service"

    _tz = cfg.overlay.timezone
    if _tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(_tz)
        except (KeyError, Exception):
            log.warning("Invalid timezone '%s', falling back to UTC", _tz)
            cfg.overlay.timezone = "UTC"
    cfg.pco.poll_interval = max(2, min(cfg.pco.poll_interval, 60))
    if cfg.pco.search_mode not in ("service_type", "folder"):
        cfg.pco.search_mode = "service_type"
    cfg.web.port = max(1, min(cfg.web.port, 65535))

    # Stream max resolution validation
    _allowed_max_res = {"best", "2160", "1440", "1080", "720", "480"}
    if cfg.stream.max_resolution not in _allowed_max_res:
        cfg.stream.max_resolution = "1080"

    # HDMI resolution validation — tightened ranges and known refresh rates
    _valid_refresh = {24, 25, 30, 50, 60, 120}
    _hdmi_m = re.match(r'^(\d+)x(\d+)(?:@(\d+)(D)?)?$', cfg.display.hdmi_resolution)
    if _hdmi_m:
        _w, _h = int(_hdmi_m.group(1)), int(_hdmi_m.group(2))
        _rate = int(_hdmi_m.group(3)) if _hdmi_m.group(3) else 60
        if not (320 <= _w <= 7680 and 240 <= _h <= 4320 and _rate in _valid_refresh):
            cfg.display.hdmi_resolution = "1920x1080@60D"
    else:
        cfg.display.hdmi_resolution = "1920x1080@60D"

    # Static IP validation
    _validate_static_ip(cfg.network, "eth")
    _validate_static_ip(cfg.network, "wifi")


def load_config(path: str | Path | None = None) -> Config:
    """Read TOML config, return validated Config.  Missing keys get defaults."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = Config()

    if path.exists():
        try:
            with open(path, "rb") as fp:
                raw = tomllib.load(fp)
            if "general" in raw:
                _apply_dict(cfg.general, raw["general"])
            if "stream" in raw:
                _apply_dict(cfg.stream, raw["stream"])
            if "overlay" in raw:
                _apply_dict(cfg.overlay, raw["overlay"])
            if "pco" in raw:
                _apply_dict(cfg.pco, raw["pco"])
            if "web" in raw:
                _apply_dict(cfg.web, raw["web"])
            if "network" in raw:
                _apply_dict(cfg.network, raw["network"])
            if "display" in raw:
                _apply_dict(cfg.display, raw["display"])
        except Exception:
            log.exception("Failed to parse config at %s — using defaults", path)
    else:
        log.warning("Config file %s not found — using defaults", path)

    validate_config(cfg)

    return cfg


def to_dict_safe(cfg: Config) -> dict:
    """Export config as a dict, stripping sensitive fields."""
    data = {
        "general": _section_to_dict(cfg.general),
        "stream": _section_to_dict(cfg.stream),
        "overlay": _section_to_dict(cfg.overlay),
        "pco": _section_to_dict(cfg.pco),
        "web": _section_to_dict(cfg.web),
        "network": _section_to_dict(cfg.network),
        "display": _section_to_dict(cfg.display),
    }
    # Strip secrets
    data["pco"].pop("secret", None)
    return data


def save_config(cfg: Config, path: str | Path | None = None) -> None:
    """Write Config back to TOML atomically with backup."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "general": _section_to_dict(cfg.general),
        "stream": _section_to_dict(cfg.stream),
        "overlay": _section_to_dict(cfg.overlay),
        "pco": _section_to_dict(cfg.pco),
        "web": _section_to_dict(cfg.web),
        "network": _section_to_dict(cfg.network),
        "display": _section_to_dict(cfg.display),
    }

    # Backup existing config before overwrite
    if path.exists():
        try:
            shutil.copy2(path, path.parent / (path.name + ".bak"))
        except OSError:
            log.warning("Could not create config backup")

    # Atomic write: temp file + os.replace
    tmp_path = path.parent / (path.name + ".tmp")
    try:
        with open(tmp_path, "wb") as fp:
            tomli_w.dump(data, fp)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # secure permissions (ignore errors on non-Linux)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
