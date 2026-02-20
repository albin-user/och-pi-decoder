"""Tests for configuration management."""

import pytest
from pathlib import Path

import json

from pi_decoder.config import (
    Config,
    GeneralConfig,
    StreamConfig,
    OverlayConfig,
    PCOConfig,
    WebConfig,
    NetworkConfig,
    DisplayConfig,
    load_config,
    save_config,
    to_dict_safe,
)


class TestConfigDefaults:
    """Test that default configuration values are correct."""

    def test_stream_defaults(self):
        cfg = StreamConfig()
        assert cfg.url == ""
        assert cfg.network_caching == 2000
        assert cfg.hwdec == "auto"
        assert cfg.max_resolution == "1080"

    def test_overlay_defaults(self):
        cfg = OverlayConfig()
        assert cfg.enabled is False
        assert cfg.position == "bottom-right"
        assert cfg.font_size == 96
        assert cfg.font_size_title == 38
        assert cfg.font_size_info == 32
        assert cfg.transparency == 0.7
        assert cfg.timer_mode == "service"
        assert cfg.show_description is False
        assert cfg.show_service_end is True
        assert cfg.timezone == "Europe/Copenhagen"

    def test_pco_defaults(self):
        cfg = PCOConfig()
        assert cfg.app_id == ""
        assert cfg.secret == ""
        assert cfg.service_type_id == ""
        assert cfg.folder_id == ""
        assert cfg.poll_interval == 5

    def test_web_defaults(self):
        cfg = WebConfig()
        assert cfg.port == 80

    def test_general_defaults(self):
        cfg = GeneralConfig()
        assert cfg.name == "Pi-Decoder"

    def test_network_defaults(self):
        cfg = NetworkConfig()
        assert cfg.hotspot_ssid == "Pi-Decoder"
        assert cfg.hotspot_password == "pidecodersetup"
        assert cfg.ethernet_timeout == 10
        assert cfg.wifi_timeout == 40

    def test_display_defaults(self):
        cfg = DisplayConfig()
        assert cfg.hdmi_resolution == "1920x1080@60D"

    def test_full_config_defaults(self):
        cfg = Config()
        assert isinstance(cfg.general, GeneralConfig)
        assert isinstance(cfg.stream, StreamConfig)
        assert isinstance(cfg.overlay, OverlayConfig)
        assert isinstance(cfg.pco, PCOConfig)
        assert isinstance(cfg.web, WebConfig)
        assert isinstance(cfg.network, NetworkConfig)
        assert isinstance(cfg.display, DisplayConfig)


class TestLoadConfig:
    """Test configuration loading."""

    def test_load_missing_file_returns_defaults(self, tmp_path: Path):
        """Loading a non-existent file should return defaults."""
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg.stream.url == ""
        assert cfg.overlay.enabled is False

    def test_load_missing_general_and_network_gets_defaults(self, tmp_config: Path):
        """Old configs without [general] or [network] sections get defaults."""
        tmp_config.write_text("""
[stream]
url = "http://old.local/stream"
""")
        cfg = load_config(tmp_config)
        assert cfg.general.name == "Pi-Decoder"
        assert cfg.network.hotspot_ssid == "Pi-Decoder"
        assert cfg.network.ethernet_timeout == 10

    def test_load_partial_config(self, tmp_config: Path):
        """Loading a partial config should merge with defaults."""
        tmp_config.write_text("""
[stream]
url = "http://custom.local/stream"

[overlay]
enabled = true
""")
        cfg = load_config(tmp_config)
        assert cfg.stream.url == "http://custom.local/stream"
        assert cfg.stream.network_caching == 2000  # default
        assert cfg.overlay.enabled is True
        assert cfg.overlay.font_size == 96  # default

    def test_load_full_config(self, tmp_config: Path):
        """Loading a full config should use all values."""
        tmp_config.write_text("""
[stream]
url = "http://test.local/stream.m3u8"
network_caching = 3000

[overlay]
enabled = true
position = "top-left"
font_size = 72
transparency = 0.5
timer_mode = "item"
show_description = false
show_service_end = false
timezone = "America/New_York"

[pco]
app_id = "test_id"
secret = "test_secret"
service_type_id = "12345"
folder_id = "898807"
poll_interval = 10

[web]
port = 8080
""")
        cfg = load_config(tmp_config)
        assert cfg.stream.url == "http://test.local/stream.m3u8"
        assert cfg.stream.network_caching == 3000
        assert cfg.overlay.enabled is True
        assert cfg.overlay.position == "top-left"
        assert cfg.overlay.font_size == 72
        assert cfg.overlay.transparency == 0.5
        assert cfg.overlay.timer_mode == "item"
        assert cfg.overlay.show_description is False
        assert cfg.pco.app_id == "test_id"
        assert cfg.pco.folder_id == "898807"
        assert cfg.pco.poll_interval == 10
        assert cfg.web.port == 8080


    def test_load_general_and_network_sections(self, tmp_config: Path):
        """Loading general and network sections should work."""
        tmp_config.write_text("""
[general]
name = "Main Sanctuary"

[network]
hotspot_ssid = "MyChurch"
hotspot_password = "setup123"
ethernet_timeout = 15
wifi_timeout = 30
""")
        cfg = load_config(tmp_config)
        assert cfg.general.name == "Main Sanctuary"
        assert cfg.network.hotspot_ssid == "MyChurch"
        assert cfg.network.hotspot_password == "setup123"
        assert cfg.network.ethernet_timeout == 15
        assert cfg.network.wifi_timeout == 30


class TestConfigValidation:
    """Test configuration value validation."""

    def test_network_caching_clamped(self, tmp_config: Path):
        """Network caching should be clamped to valid range."""
        tmp_config.write_text("""
[stream]
network_caching = 100
""")
        cfg = load_config(tmp_config)
        assert cfg.stream.network_caching == 200  # minimum

        tmp_config.write_text("""
[stream]
network_caching = 50000
""")
        cfg = load_config(tmp_config)
        assert cfg.stream.network_caching == 30000  # maximum

    def test_font_size_clamped(self, tmp_config: Path):
        """Font size should be clamped to valid range."""
        tmp_config.write_text("""
[overlay]
font_size = 5
""")
        cfg = load_config(tmp_config)
        assert cfg.overlay.font_size == 10  # minimum

        tmp_config.write_text("""
[overlay]
font_size = 300
""")
        cfg = load_config(tmp_config)
        assert cfg.overlay.font_size == 200  # maximum

    def test_transparency_clamped(self, tmp_config: Path):
        """Transparency should be clamped to 0.0-1.0."""
        tmp_config.write_text("""
[overlay]
transparency = -0.5
""")
        cfg = load_config(tmp_config)
        assert cfg.overlay.transparency == 0.0

        tmp_config.write_text("""
[overlay]
transparency = 1.5
""")
        cfg = load_config(tmp_config)
        assert cfg.overlay.transparency == 1.0

    def test_invalid_position_defaults(self, tmp_config: Path):
        """Invalid position should default to bottom-right."""
        tmp_config.write_text("""
[overlay]
position = "invalid"
""")
        cfg = load_config(tmp_config)
        assert cfg.overlay.position == "bottom-right"

    def test_invalid_timer_mode_defaults(self, tmp_config: Path):
        """Invalid timer_mode should default to service."""
        tmp_config.write_text("""
[overlay]
timer_mode = "invalid"
""")
        cfg = load_config(tmp_config)
        assert cfg.overlay.timer_mode == "service"

    def test_poll_interval_clamped(self, tmp_config: Path):
        """Poll interval should be clamped to valid range."""
        tmp_config.write_text("""
[pco]
poll_interval = 1
""")
        cfg = load_config(tmp_config)
        assert cfg.pco.poll_interval == 2  # minimum

    def test_web_port_clamped(self, tmp_config: Path):
        """Web port should be clamped to valid range."""
        tmp_config.write_text("""
[web]
port = 0
""")
        cfg = load_config(tmp_config)
        assert cfg.web.port == 1  # minimum

    def test_invalid_max_resolution_defaults(self, tmp_config: Path):
        """Invalid max_resolution should default to 1080."""
        tmp_config.write_text("""
[stream]
max_resolution = "4k"
""")
        cfg = load_config(tmp_config)
        assert cfg.stream.max_resolution == "1080"

    def test_valid_max_resolution_preserved(self, tmp_config: Path):
        """Valid max_resolution values should be preserved."""
        tmp_config.write_text("""
[stream]
max_resolution = "720"
""")
        cfg = load_config(tmp_config)
        assert cfg.stream.max_resolution == "720"

    def test_invalid_hdmi_resolution_defaults(self, tmp_config: Path):
        """Invalid hdmi_resolution should default to 1920x1080@60D."""
        tmp_config.write_text("""
[display]
hdmi_resolution = "not-a-resolution"
""")
        cfg = load_config(tmp_config)
        assert cfg.display.hdmi_resolution == "1920x1080@60D"

    def test_valid_hdmi_resolution_preserved(self, tmp_config: Path):
        """Valid hdmi_resolution values should be preserved."""
        tmp_config.write_text("""
[display]
hdmi_resolution = "1280x720@50D"
""")
        cfg = load_config(tmp_config)
        assert cfg.display.hdmi_resolution == "1280x720@50D"

    def test_network_timeouts_clamped(self, tmp_config: Path):
        """Network timeouts should be clamped to valid range."""
        tmp_config.write_text("""
[network]
ethernet_timeout = 0
wifi_timeout = 999
""")
        cfg = load_config(tmp_config)
        assert cfg.network.ethernet_timeout == 1  # minimum
        assert cfg.network.wifi_timeout == 120  # maximum


class TestStaticIpValidation:
    """Test static IP validation in config."""

    def test_static_ip_defaults(self):
        cfg = NetworkConfig()
        assert cfg.eth_ip_mode == "auto"
        assert cfg.eth_ip_address == ""
        assert cfg.wifi_ip_mode == "auto"

    def test_valid_static_ip_roundtrip(self, tmp_config: Path):
        tmp_config.write_text("""
[network]
eth_ip_mode = "manual"
eth_ip_address = "192.168.1.100/24"
eth_gateway = "192.168.1.1"
eth_dns = "8.8.8.8, 8.8.4.4"
""")
        cfg = load_config(tmp_config)
        assert cfg.network.eth_ip_mode == "manual"
        assert cfg.network.eth_ip_address == "192.168.1.100/24"
        assert cfg.network.eth_gateway == "192.168.1.1"
        assert cfg.network.eth_dns == "8.8.8.8, 8.8.4.4"

    def test_invalid_ip_reverts_to_dhcp(self, tmp_config: Path):
        tmp_config.write_text("""
[network]
eth_ip_mode = "manual"
eth_ip_address = "not-an-ip"
eth_gateway = "192.168.1.1"
eth_dns = "8.8.8.8"
""")
        cfg = load_config(tmp_config)
        assert cfg.network.eth_ip_mode == "auto"
        assert cfg.network.eth_ip_address == ""
        assert cfg.network.eth_gateway == ""
        assert cfg.network.eth_dns == ""

    def test_invalid_gateway_cleared(self, tmp_config: Path):
        tmp_config.write_text("""
[network]
eth_ip_mode = "manual"
eth_ip_address = "192.168.1.100/24"
eth_gateway = "not.valid"
eth_dns = "8.8.8.8"
""")
        cfg = load_config(tmp_config)
        assert cfg.network.eth_ip_mode == "manual"
        assert cfg.network.eth_gateway == ""
        assert cfg.network.eth_dns == "8.8.8.8"

    def test_invalid_dns_entries_skipped(self, tmp_config: Path):
        tmp_config.write_text("""
[network]
wifi_ip_mode = "manual"
wifi_ip_address = "10.0.0.5/16"
wifi_gateway = "10.0.0.1"
wifi_dns = "8.8.8.8, bad-dns, 1.1.1.1"
""")
        cfg = load_config(tmp_config)
        assert cfg.network.wifi_ip_mode == "manual"
        assert cfg.network.wifi_dns == "8.8.8.8, 1.1.1.1"

    def test_empty_address_reverts_to_dhcp(self, tmp_config: Path):
        tmp_config.write_text("""
[network]
eth_ip_mode = "manual"
eth_ip_address = ""
""")
        cfg = load_config(tmp_config)
        assert cfg.network.eth_ip_mode == "auto"

    def test_invalid_mode_reverts_to_auto(self, tmp_config: Path):
        tmp_config.write_text("""
[network]
eth_ip_mode = "bogus"
""")
        cfg = load_config(tmp_config)
        assert cfg.network.eth_ip_mode == "auto"


class TestSaveConfig:
    """Test configuration saving."""

    def test_save_and_load_roundtrip(self, tmp_config: Path):
        """Saving and loading should preserve values."""
        cfg = Config()
        cfg.general.name = "Test Decoder"
        cfg.stream.url = "http://roundtrip.local/stream"
        cfg.stream.max_resolution = "720"
        cfg.overlay.enabled = True
        cfg.overlay.font_size = 80
        cfg.pco.app_id = "saved_id"
        cfg.web.port = 9000
        cfg.network.hotspot_ssid = "TestHotspot"
        cfg.network.wifi_timeout = 30
        cfg.display.hdmi_resolution = "1280x720@50D"

        save_config(cfg, tmp_config)
        loaded = load_config(tmp_config)

        assert loaded.general.name == "Test Decoder"
        assert loaded.stream.url == "http://roundtrip.local/stream"
        assert loaded.stream.max_resolution == "720"
        assert loaded.overlay.enabled is True
        assert loaded.overlay.font_size == 80
        assert loaded.pco.app_id == "saved_id"
        assert loaded.web.port == 9000
        assert loaded.network.hotspot_ssid == "TestHotspot"
        assert loaded.network.wifi_timeout == 30
        assert loaded.display.hdmi_resolution == "1280x720@50D"

    def test_save_creates_parent_directory(self, tmp_path: Path):
        """Saving should create parent directories if needed."""
        config_path = tmp_path / "subdir" / "config.toml"
        cfg = Config()
        save_config(cfg, config_path)
        assert config_path.exists()

    def test_backup_url_roundtrip(self, tmp_config: Path):
        """Save and load should preserve backup_url."""
        cfg = Config()
        cfg.stream.url = "rtmp://primary.local/live"
        cfg.stream.backup_url = "rtmp://backup.local/live"
        save_config(cfg, tmp_config)
        loaded = load_config(tmp_config)
        assert loaded.stream.backup_url == "rtmp://backup.local/live"

    def test_presets_roundtrip(self, tmp_config: Path):
        """Save and load should preserve presets list-of-dicts."""
        cfg = Config()
        cfg.stream.presets = [
            {"label": "Church", "url": "rtmp://church.local/live"},
            {"label": "Backup", "url": "rtmp://backup.local/live"},
        ]
        save_config(cfg, tmp_config)
        loaded = load_config(tmp_config)
        assert len(loaded.stream.presets) == 2
        assert loaded.stream.presets[0]["label"] == "Church"
        assert loaded.stream.presets[1]["url"] == "rtmp://backup.local/live"


class TestToDictSafe:
    """Test to_dict_safe secret stripping."""

    def test_secret_not_in_output(self):
        cfg = Config()
        cfg.pco.app_id = "my_app_id"
        cfg.pco.secret = "super_secret_value"
        data = to_dict_safe(cfg)
        assert "secret" not in data["pco"]
        serialized = json.dumps(data)
        assert "super_secret_value" not in serialized

    def test_app_id_preserved(self):
        cfg = Config()
        cfg.pco.app_id = "my_app_id"
        cfg.pco.secret = "super_secret_value"
        data = to_dict_safe(cfg)
        assert data["pco"]["app_id"] == "my_app_id"

    def test_other_sections_present(self):
        cfg = Config()
        data = to_dict_safe(cfg)
        for section in ("general", "stream", "overlay", "pco", "web", "network", "display"):
            assert section in data
