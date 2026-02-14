"""Configuration manager — TOML load/save/validate."""

from __future__ import annotations

import logging
import os
import sys
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
    network_caching: int = 2000


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
class DisplayConfig:
    hide_cursor: bool = True


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


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    pco: PCOConfig = field(default_factory=PCOConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    web: WebConfig = field(default_factory=WebConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)


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
            setattr(dc, f.name, val)


def _section_to_dict(dc: object) -> dict:
    """Convert a dataclass section to a plain dict (one level)."""
    return {f.name: getattr(dc, f.name) for f in fields(dc)}  # type: ignore[arg-type]


# ── public API ───────────────────────────────────────────────────────────────


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
            if "display" in raw:
                _apply_dict(cfg.display, raw["display"])
            if "web" in raw:
                _apply_dict(cfg.web, raw["web"])
            if "network" in raw:
                _apply_dict(cfg.network, raw["network"])
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
        "display": _section_to_dict(cfg.display),
        "web": _section_to_dict(cfg.web),
        "network": _section_to_dict(cfg.network),
    }
    # Strip secrets
    data["pco"].pop("secret", None)
    return data


def save_config(cfg: Config, path: str | Path | None = None) -> None:
    """Write Config back to TOML."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "general": _section_to_dict(cfg.general),
        "stream": _section_to_dict(cfg.stream),
        "overlay": _section_to_dict(cfg.overlay),
        "pco": _section_to_dict(cfg.pco),
        "display": _section_to_dict(cfg.display),
        "web": _section_to_dict(cfg.web),
        "network": _section_to_dict(cfg.network),
    }

    with open(path, "wb") as fp:
        tomli_w.dump(data, fp)

    # secure permissions (ignore errors on non-Linux)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
