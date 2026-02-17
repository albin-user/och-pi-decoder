# Pi-Decoder

A Raspberry Pi-based video decoder with Planning Center Online (PCO) countdown overlay integration. Designed for churches to display live video streams with real-time service countdown timers.

## Features

- **Hardware-accelerated video playback** via mpv with H.265/HEVC support
- **HLS/RTMP stream decoding** with automatic reconnection and resilience
- **Planning Center Online integration** for real-time service countdown timers
- **ASS subtitle overlays** for smooth, GPU-rendered countdown display
- **Web-based configuration UI** - no SSH required for day-to-day operation
- **CEC TV control** - power on/off, input switching, and volume control over HDMI
- **Config backup/restore** - export and import TOML configuration for multi-unit deployment
- **Dark mode** - automatic dark theme based on OS preference
- **Auto-recovery** - survives encoder outages, network drops, and reboots
- **Bitfocus Companion compatible** - expose stream status via HTTP API

## Quick Links

| Document | Description |
|----------|-------------|
| [DOCUMENTATION.md](DOCUMENTATION.md) | Full deployment guide and troubleshooting |
| [STREAM_RESILIENCE_IMPLEMENTATION.md](STREAM_RESILIENCE_IMPLEMENTATION.md) | Stream auto-retry feature details |
| [deploy/README.md](deploy/README.md) | Quick deployment steps |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Raspberry Pi 5                                   │
│                                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                  │
│  │   FastAPI    │◄──►│  MpvManager  │◄──►│     mpv      │──► HDMI Out      │
│  │   Web App    │    │              │    │   (video)    │                  │
│  └──────────────┘    └──────────────┘    └──────────────┘                  │
│         │                   ▲                   ▲                           │
│         │                   │                   │                           │
│         ▼                   │            JSON IPC Socket                    │
│  ┌──────────────┐    ┌──────────────┐          │                           │
│  │  PCOClient   │───►│   Overlay    │──────────┘                           │
│  │  (API calls) │    │   Updater    │   (ASS subtitles)                    │
│  └──────────────┘    └──────────────┘                                      │
│         │                                                                   │
└─────────│───────────────────────────────────────────────────────────────────┘
          │
          ▼
   Planning Center
   Online Services API
```

### Data Flow

1. **Video Stream**: Encoder → (HLS/RTMP) → mpv → HDMI output
2. **Overlay Data**: PCO API → PCOClient → OverlayUpdater → mpv (ASS overlay)
3. **Configuration**: Web UI → FastAPI → Config file + component updates
4. **Health Monitoring**: MpvManager health loop → auto-restart on failure

---

## Module Reference

### Source Files (`src/pi_decoder/`)

| File | Purpose |
|------|---------|
| `main.py` | Application entry point, component orchestration, signal handling |
| `config.py` | TOML configuration load/save, validation, dataclass definitions |
| `mpv_manager.py` | mpv process lifecycle, JSON IPC client, health monitoring, stream retry |
| `pco_client.py` | Planning Center Online API client, live service status polling |
| `overlay.py` | ASS subtitle formatting, countdown display, overlay push loop |
| `web/app.py` | FastAPI REST API, WebSocket endpoints, web UI templates |

### Key Classes

#### `MpvManager`
Manages the mpv media player subprocess:
- Starts mpv with DRM output for direct hardware rendering (no desktop required)
- Communicates via JSON IPC over Unix socket (`/tmp/mpv-pi-decoder.sock`)
- Health monitoring loop auto-restarts crashed processes
- Stream retry logic with exponential backoff (5s → 60s max)

#### `PCOClient`
Async client for Planning Center Services Live API:
- Discovers service types via folder or service-type search mode (configurable)
- Uses IDLE/SCANNING/TRACKING state machine for efficient polling
- Picks the plan with the most recent `live_start_at` when multiple plans are live (abandoned-session detection)
- Locks onto active plans to minimize API calls (1 `/live` call per cycle when tracking)
- Computes accurate countdown timers from live data

#### `OverlayUpdater`
Bridges PCO status to mpv overlay display:
- Polls PCO every N seconds (configurable)
- Pushes ASS overlay to mpv every 1 second for smooth countdowns
- Handles timezone conversion for "ends at HH:MM" display
- Color-codes overtime (red) vs on-time (white)

#### `Config`
Dataclass-based configuration with TOML persistence:
- Validates and clamps values to safe ranges
- Supports hot-reload via web API
- Secure file permissions (0600)

---

## REST API Reference

Base URL: `http://<PI-IP>/` (default port 80)

All POST endpoints accept `application/json` unless noted. All fields are optional — only send the fields you want to change. Responses return `{"ok": true}` on success or `{"ok": false, "error": "..."}` on failure.

### Status & Info

#### `GET /api/health`
Health check.

**Response:** `{"status": "ok"}`

#### `GET /api/status`
Full system status including video player, overlay, system metrics, and network.

**Response:**
```json
{
  "name": "Pi-Decoder",
  "mpv": {
    "alive": true,
    "playing": true,
    "idle": false,
    "stream_url": "rtmp://encoder.local/live/stream"
  },
  "overlay": {
    "enabled": true,
    "credentials_set": true,
    "is_live": true,
    "finished": false,
    "plan_title": "Sunday Service",
    "item_title": "Worship",
    "countdown": "01:23:45",
    "message": ""
  },
  "system": {
    "cpu_percent": 12.5,
    "memory_percent": 45.2,
    "memory_used_mb": 1024,
    "memory_total_mb": 4096,
    "temperature": 52.3,
    "uptime": "2d 5h",
    "uptime_seconds": 190800
  },
  "network": {
    "connection_type": "ethernet",
    "ip": "192.168.1.100",
    "ssid": "",
    "hotspot_active": false,
    "signal": 0
  }
}
```

#### `GET /api/version`
Installed package version.

**Response:** `{"version": "1.0.0"}`

#### `GET /api/screenshot`
Captures current HDMI output (video + overlays) as JPEG.

**Response:** `image/jpeg` binary data, or `500` with `{"ok": false, "error": "Screenshot failed"}`

#### `GET /api/logs?service=pi-decoder&lines=50`
Recent systemd journal logs.

**Parameters:**
- `service` (string, default: `pi-decoder`) — systemd unit name
- `lines` (int, default: 50, max: 1000) — number of log lines

**Response:** `{"service": "pi-decoder", "logs": "..."}`

### Configuration

#### `POST /api/config/general`
Set decoder name (also updates system hostname).

**Request:**
```json
{"name": "Sanctuary"}
```

**Response:** `{"ok": true, "hostname": "sanctuary"}`

#### `POST /api/config/stream`
Update stream settings. Automatically restarts video on save from the web UI.

**Request:**
```json
{
  "url": "rtmp://encoder.local/live/stream",
  "network_caching": 2000
}
```

#### `POST /api/config/overlay`
Update overlay appearance settings.

**Request:**
```json
{
  "enabled": true,
  "position": "bottom-right",
  "font_size": 96,
  "font_size_title": 38,
  "font_size_info": 32,
  "transparency": 0.7,
  "timer_mode": "service",
  "show_description": false,
  "show_service_end": true,
  "timezone": "America/Chicago"
}
```

#### `POST /api/config/pco`
Update PCO credentials and search settings.

**Request:**
```json
{
  "app_id": "your-app-id",
  "secret": "your-secret",
  "service_type_id": "123456",
  "search_mode": "service_type",
  "folder_id": "",
  "poll_interval": 5
}
```

#### `POST /api/config/network`
Update all network settings: hotspot, boot timeouts, and static IP configuration.

**Request:**
```json
{
  "hotspot_ssid": "Pi-Decoder",
  "hotspot_password": "pidecodersetup",
  "ethernet_timeout": 10,
  "wifi_timeout": 40,
  "eth_ip_mode": "manual",
  "eth_ip_address": "192.168.1.100/24",
  "eth_gateway": "192.168.1.1",
  "eth_dns": "8.8.8.8, 8.8.4.4",
  "wifi_ip_mode": "auto",
  "wifi_ip_address": "",
  "wifi_gateway": "",
  "wifi_dns": ""
}
```

`*_ip_mode` accepts `"auto"` (DHCP) or `"manual"` (static). When manual, provide `*_ip_address` in CIDR notation (e.g. `192.168.1.100/24`). This saves the config — call `/api/network/apply-ip` to activate it.

#### `GET /api/config/export`
Download the current configuration as a TOML file. Sensitive fields (`pco.secret`) are stripped.

**Response:** `application/toml` file download

#### `POST /api/config/import`
Upload a TOML configuration file. Validates and merges settings, preserving existing secrets.

**Request:** `multipart/form-data` with `file` field containing a `.toml` file (max 64 KB)

### PCO (Planning Center Online)

#### `POST /api/test-pco`
Test PCO credentials without saving.

**Request:**
```json
{
  "app_id": "your-app-id",
  "secret": "your-secret",
  "service_type_id": "123456"
}
```

**Response:**
```json
{
  "success": true,
  "service_types": [
    {"id": "123456", "name": "Sunday Service"}
  ]
}
```

#### `GET /api/service-types`
List available PCO service types (requires saved credentials).

**Response:** `{"service_types": [{"id": "123456", "name": "Sunday Service"}]}`

### Network

#### `GET /api/network/status`
Current network connection info.

**Response:**
```json
{
  "connection_type": "wifi",
  "ip": "192.168.1.100",
  "ssid": "ChurchWiFi",
  "hotspot_active": false,
  "signal": 72
}
```

`connection_type` is one of: `"ethernet"`, `"wifi"`, `"hotspot"`, `"none"`.

#### `GET /api/network/wifi-scan`
Scan for available WiFi networks (~2s delay for radio scan).

**Response:**
```json
{
  "networks": [
    {"ssid": "ChurchWiFi", "signal": 85, "security": "WPA2", "in_use": true},
    {"ssid": "Guest", "signal": 45, "security": "WPA2", "in_use": false}
  ]
}
```

#### `POST /api/network/wifi-connect`
Connect to a WiFi network. Stops hotspot first if active.

**Request:**
```json
{"ssid": "ChurchWiFi", "password": "mypassword"}
```

SSID must be 1-32 bytes. Password must be 8-63 characters (or empty for open networks).

#### `GET /api/network/wifi/saved`
List saved WiFi connection names.

**Response:** `{"networks": ["ChurchWiFi", "Office"]}`

#### `POST /api/network/wifi/forget`
Delete a saved WiFi connection.

**Request:** `{"name": "OldNetwork"}`

#### `POST /api/network/hotspot/start`
Start the WiFi hotspot. **Blocked with 400** if Ethernet or WiFi is currently active (single-interface policy).

#### `POST /api/network/hotspot/stop`
Stop the WiFi hotspot.

#### `POST /api/network/apply-ip`
Apply the saved static IP configuration to an active connection. Call this after saving settings with `/api/config/network`. The connection briefly drops (~1-2s) while re-activating.

**Request:**
```json
{"interface": "ethernet"}
```

`interface` must be `"ethernet"` or `"wifi"`. Returns 500 if no active connection exists for that interface.

**Example — set static IP via API:**
```bash
# 1. Save the config
curl -X POST http://pi-decoder.local/api/config/network \
  -H 'Content-Type: application/json' \
  -d '{"eth_ip_mode":"manual","eth_ip_address":"192.168.1.100/24","eth_gateway":"192.168.1.1","eth_dns":"8.8.8.8"}'

# 2. Apply it
curl -X POST http://pi-decoder.local/api/network/apply-ip \
  -H 'Content-Type: application/json' \
  -d '{"interface":"ethernet"}'
```

#### `GET /api/network/speedtest`
Get the last speed test result (or `null` if never run).

**Response:** `{"ok": true, "result": {"download_mbps": 47.2, "latency_ms": 12.0, "timestamp": "2025-01-15T10:30:00", "wifi_band": "5 GHz", "avg_signal": 72, "interface_type": "USB adapter"}}`

#### `POST /api/network/speedtest`
Run a download speed test (~10s, downloads ~10 MB from Cloudflare). Returns 409 if already in progress.

**Response:** `{"ok": true, "download_mbps": 47.2, "latency_ms": 12.0, "timestamp": "...", "wifi_band": "5 GHz", "avg_signal": 72, "interface_type": "USB adapter"}`

### CEC TV Control

All CEC endpoints control the TV over HDMI using the CEC protocol. Useful for automation with Bitfocus Companion, Home Assistant, etc.

#### `POST /api/cec/on`
Power on the TV.

#### `POST /api/cec/standby`
Put the TV in standby (off).

#### `GET /api/cec/power-status`
Get TV power status.

**Response:** `{"ok": true, "status": "on"}` — status is `"on"`, `"standby"`, or `"unknown"`.

#### `POST /api/cec/active-source`
Switch the TV to the Pi's HDMI input.

#### `POST /api/cec/input`
Switch the TV to a specific HDMI port.

**Request:** `{"port": 2}` — port 1-4.

#### `POST /api/cec/volume-up`
TV volume up.

#### `POST /api/cec/volume-down`
TV volume down.

#### `POST /api/cec/mute`
Toggle TV mute.

### Video Controls

#### `POST /api/stop/video`
Stop video playback (shows idle screen).

#### `POST /api/restart/video`
Restart the video player process.

#### `POST /api/restart/overlay`
Restart the PCO overlay updater.

#### `POST /api/restart/all`
Restart both video player and overlay.

### System

#### `POST /api/update`
Upload and install a software update package.

**Request:** `multipart/form-data` with `file` field containing a `.whl` or `.tar.gz` file (max 10 MB). The service restarts automatically after install.

**Response:** `{"ok": true, "version": "1.1.0", "message": "Updated to 1.1.0, restarting..."}`

#### `POST /api/reboot`
Reboot the Raspberry Pi (~90s downtime).

#### `POST /api/shutdown`
Shut down the Raspberry Pi. Requires physical access to power it back on.

### WebSocket Endpoints

#### `WS /ws/status`
Real-time status updates. Sends a JSON message every 2 seconds with the same shape as `GET /api/status` plus `hostname` and `cec.power` fields. The `network.hotspot_password` field is stripped from broadcasts.

#### `WS /ws/preview`
Live video preview. Sends binary JPEG frames every 2 seconds.

---

## Configuration Reference

Configuration file: `/etc/pi-decoder/config.toml`

### `[general]` Section

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | `"Pi-Decoder"` | Device name shown in UI and used for hostname |

### `[stream]` Section

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `url` | string | `""` | - | HLS/RTMP stream URL |
| `network_caching` | int | `2000` | 200-30000 | Buffer size in milliseconds |

### `[overlay]` Section

| Key | Type | Default | Options/Range | Description |
|-----|------|---------|---------------|-------------|
| `enabled` | bool | `false` | - | Enable PCO countdown overlay |
| `position` | string | `"bottom-right"` | `top-left`, `top-right`, `bottom-left`, `bottom-right` | Screen corner for overlay |
| `font_size` | int | `56` | 10-200 | Base font size in pixels |
| `transparency` | float | `0.7` | 0.0-1.0 | Overlay opacity (0=invisible, 1=opaque) |
| `timer_mode` | string | `"service"` | `service`, `item` | Countdown to service end or current item end |
| `show_description` | bool | `true` | - | Show item description (item mode only) |
| `show_service_end` | bool | `true` | - | Show "Ends at HH:MM" line |
| `timezone` | string | `"Europe/Copenhagen"` | IANA timezone | Timezone for end time display |

### `[pco]` Section

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `app_id` | string | `""` | - | PCO Personal Access Token App ID |
| `secret` | string | `""` | - | PCO Personal Access Token Secret |
| `service_type_id` | string | `""` | - | PCO Service Type ID |
| `folder_id` | string | `""` | - | PCO Folder ID — discovers all service types in this folder |
| `search_mode` | string | `"service_type"` | `service_type`, `folder` | How to discover services in PCO |
| `poll_interval` | int | `5` | 2-60 | Seconds between PCO API polls |

### `[web]` Section

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `port` | int | `80` | 1-65535 | Web server port |

### `[network]` Section

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `hotspot_ssid` | string | `"Pi-Decoder"` | - | WiFi hotspot network name |
| `hotspot_password` | string | `"pidecodersetup"` | - | WiFi hotspot password |
| `ethernet_timeout` | int | `10` | 1-120 | Seconds to wait for Ethernet before fallback |
| `wifi_timeout` | int | `40` | 1-120 | Seconds to wait for WiFi before hotspot fallback |
| `eth_ip_mode` | string | `"auto"` | `auto`, `manual` | Ethernet IP mode (DHCP or static) |
| `eth_ip_address` | string | `""` | CIDR | Static IP for Ethernet (e.g. `192.168.1.100/24`) |
| `eth_gateway` | string | `""` | IPv4 | Default gateway for Ethernet |
| `eth_dns` | string | `""` | IPv4 CSV | DNS servers for Ethernet (comma-separated) |
| `wifi_ip_mode` | string | `"auto"` | `auto`, `manual` | WiFi IP mode (DHCP or static) |
| `wifi_ip_address` | string | `""` | CIDR | Static IP for WiFi (e.g. `10.0.0.50/24`) |
| `wifi_gateway` | string | `""` | IPv4 | Default gateway for WiFi |
| `wifi_dns` | string | `""` | IPv4 CSV | DNS servers for WiFi (comma-separated) |

**Network behavior:** Only one connection is active at a time (Ethernet > WiFi > Hotspot). When Ethernet connects, WiFi is disconnected. When Ethernet drops, WiFi reconnects automatically. If all connections are lost for 30 seconds, the hotspot auto-starts. The hotspot cannot be started via API while Ethernet or WiFi is active.

### Example Configuration

```toml
[general]
name = "Sanctuary"

[stream]
url = "http://192.168.1.50:8080/stream.m3u8"
network_caching = 2000

[overlay]
enabled = true
position = "bottom-right"
font_size = 56
transparency = 0.7
timer_mode = "service"
show_description = true
show_service_end = true
timezone = "America/Chicago"

[pco]
app_id = "abc123..."
secret = "xyz789..."
service_type_id = "123456"
search_mode = "service_type"
poll_interval = 5

[web]
port = 80

[network]
hotspot_ssid = "Sanctuary-Setup"
hotspot_password = "mypassword"
ethernet_timeout = 10
wifi_timeout = 40
eth_ip_mode = "manual"
eth_ip_address = "192.168.1.100/24"
eth_gateway = "192.168.1.1"
eth_dns = "8.8.8.8, 8.8.4.4"
wifi_ip_mode = "auto"
```

---

## Development Setup

### Prerequisites

- Python 3.11+
- mpv media player (for local testing)

### Installation

```bash
# Clone the repository
git clone https://github.com/albin-user/och-pi-decoder.git
cd och-pi-decoder

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or: .venv\Scripts\activate  # Windows

# Install in development mode
pip install -e ".[dev]"
```

### Running Locally

```bash
# Set config path (optional, defaults to /etc/pi-decoder/config.toml)
export PI_DECODER_CONFIG=./config.toml

# Run the application
pi-decoder

# Or run directly
python -m pi_decoder
```

### Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/test_config.py
```

### Project Structure

```
och-pi-decoder/
├── src/
│   └── pi_decoder/
│       ├── __init__.py
│       ├── __main__.py          # python -m entry point
│       ├── main.py              # Application entry point
│       ├── config.py            # Configuration management
│       ├── mpv_manager.py       # mpv process control
│       ├── pco_client.py        # PCO API client
│       ├── overlay.py           # Countdown overlay
│       ├── cec.py              # CEC TV control via HDMI
│       └── web/
│           ├── __init__.py
│           ├── app.py           # FastAPI application
│           ├── templates/       # Jinja2 HTML templates
│           └── static/          # CSS, JavaScript
├── tests/
│   ├── conftest.py              # pytest fixtures
│   ├── test_config.py
│   ├── test_overlay.py
│   ├── test_pco_client.py
│   └── test_pigen.py
├── deploy/
│   ├── README.md
│   ├── setup.sh                 # Installation script
│   ├── pi-decoder.service       # systemd unit file
│   ├── config.toml.example      # Example configuration
│   └── pi-gen/                  # Custom SD card image builder
├── pyproject.toml               # Project metadata and dependencies
├── README.md                    # This file
├── DOCUMENTATION.md             # Deployment and troubleshooting guide
└── STREAM_RESILIENCE_IMPLEMENTATION.md
```

---

## Dependencies

### Runtime Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| fastapi | ≥0.104 | Web framework for REST API |
| uvicorn | ≥0.24 | ASGI server |
| httpx | ≥0.25 | Async HTTP client for PCO API |
| jinja2 | ≥3.1 | HTML templating |
| psutil | ≥5.9 | System monitoring |
| python-dateutil | ≥2.8 | Date/time parsing |
| tomli-w | ≥1.0 | TOML writing |

### System Dependencies (Raspberry Pi)

- `mpv` - Media player
- `python3` - Python 3.11+

---

## Bitfocus Companion Integration

The pi-decoder REST API works with Companion's **Generic HTTP** module to provide custom variables on buttons and one-press actions for TV/stream control.

### Module Setup

1. In Companion, add a **Generic HTTP** connection
2. Set the base URL to `http://<PI-IP>` (e.g. `http://192.168.1.100` or `http://pi-decoder.local`)
3. Under the module's config, add **JSON poll requests** for the endpoints below

### Custom Variables (polling)

Poll `GET /api/status` every 3-5 seconds. This single endpoint provides all the variables you need:

| JSONPath | Example value | Use for |
|----------|---------------|---------|
| `$.mpv.playing` | `true` | Button color: green if playing, red if not |
| `$.mpv.idle` | `false` | Stream down indicator |
| `$.mpv.alive` | `true` | Process health indicator |
| `$.mpv.stream_url` | `rtmp://...` | Display current stream URL |
| `$.overlay.countdown` | `01:23:45` | Show countdown on a button |
| `$.overlay.plan_title` | `Sunday Service` | Current service name |
| `$.overlay.item_title` | `Worship` | Current item name |
| `$.overlay.is_live` | `true` | PCO live indicator |
| `$.network.connection_type` | `ethernet` | Network status on a button |
| `$.network.ip` | `192.168.1.100` | Display current IP |
| `$.network.signal` | `72` | WiFi signal strength |
| `$.system.cpu_percent` | `12.5` | CPU usage |
| `$.system.temperature` | `52.3` | Pi temperature |
| `$.system.uptime` | `2d 5h` | Uptime display |
| `$.name` | `Sanctuary` | Decoder name |

For TV power status, poll `GET /api/cec/power-status` every 5-10 seconds:

| JSONPath | Example value | Use for |
|----------|---------------|---------|
| `$.status` | `on` | Button color: green if on, yellow if standby |

### Button Actions (HTTP POST)

These don't return variables — they trigger actions when a button is pressed. Use Companion's **HTTP POST** action type with no request body needed (unless noted).

| Button label | Method | URL | Body |
|-------------|--------|-----|------|
| TV On | POST | `/api/cec/on` | — |
| TV Off | POST | `/api/cec/standby` | — |
| Live Video (switch input) | POST | `/api/cec/active-source` | — |
| HDMI 2 | POST | `/api/cec/input` | `{"port": 2}` |
| Vol + | POST | `/api/cec/volume-up` | — |
| Vol - | POST | `/api/cec/volume-down` | — |
| Mute | POST | `/api/cec/mute` | — |
| Play Stream | POST | `/api/restart/video` | — |
| Stop Stream | POST | `/api/stop/video` | — |
| Restart All | POST | `/api/restart/all` | — |
| Reboot Pi | POST | `/api/reboot` | — |

For actions with a JSON body, set the Content-Type header to `application/json`.

### Example: Stream Status Button

A button that shows "LIVE" in green when the stream is playing, or "DOWN" in red when idle:

1. **Variable:** Poll `GET /api/status`, store `$.mpv.playing` as a variable
2. **Button text:** `$(generic-http:mpv_playing)` or use a conditional: show "LIVE" / "DOWN"
3. **Button color feedback:** Set green when `mpv_playing` equals `true`, red otherwise
4. **Press action:** `POST /api/restart/video` to restart the stream

### Example: Countdown Button

A button that displays the PCO service countdown timer:

1. **Variable:** Poll `GET /api/status`, store `$.overlay.countdown`
2. **Button text:** `$(generic-http:overlay_countdown)`
3. **Updates every poll interval** (3-5 seconds is fine for a countdown display)

### Notes

- **No WebSocket support needed** — HTTP polling at 3-5 second intervals works well for all use cases
- **One poll endpoint is enough** — `GET /api/status` returns everything (video, overlay, network, system) in a single request
- **All endpoints are unauthenticated** — no API keys or tokens needed, just the IP address
- The pi-decoder must be on the same network as the Companion machine (or reachable via routing)

---

## Troubleshooting

See [DOCUMENTATION.md](DOCUMENTATION.md) for comprehensive troubleshooting guide including:

- Emergency procedures for volunteers
- Common problems and solutions
- Log analysis
- Maintenance tasks

### Quick Fixes

```bash
# Check service status
sudo systemctl status pi-decoder

# View live logs
sudo journalctl -u pi-decoder -f

# Restart service
sudo systemctl restart pi-decoder

# Full reboot
sudo reboot
```

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

## Contributing

Contributions are welcome! Please open an issue to discuss your idea before submitting a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b my-feature`)
3. Run tests (`pytest -v`)
4. Commit your changes
5. Open a pull request
