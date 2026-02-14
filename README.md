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
- Starts mpv with optimized flags for kiosk display
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

Base URL: `http://<PI-IP>/`

### Status Endpoints

#### `GET /api/status`
Returns complete system status.

**Response:**
```json
{
  "mpv": {
    "alive": true,
    "paused": false,
    "idle": false,
    "playing": true,
    "stream_url": "http://encoder.local/stream.m3u8"
  },
  "overlay": {
    "enabled": true,
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
  }
}
```

#### `GET /api/screenshot`
Captures current HDMI output (video + overlays) as JPEG.

**Response:** `image/jpeg` binary data or `{"error": "Screenshot failed"}`

#### `GET /api/logs?service=pi-decoder&lines=50`
Returns recent systemd journal logs.

**Parameters:**
- `service` (string, default: `pi-decoder`) - systemd unit name
- `lines` (int, default: 50, max: 1000) - number of lines

### Configuration Endpoints

#### `POST /api/config/stream`
Update stream settings.

**Request:**
```json
{
  "url": "http://encoder.local/stream.m3u8",
  "network_caching": 2000
}
```

#### `POST /api/config/overlay`
Update overlay settings.

**Request:**
```json
{
  "enabled": true,
  "position": "bottom-right",
  "font_size": 56,
  "transparency": 0.7,
  "timer_mode": "service",
  "show_description": true,
  "show_service_end": true,
  "timezone": "America/Chicago"
}
```

#### `POST /api/config/pco`
Update PCO credentials.

**Request:**
```json
{
  "app_id": "your-app-id",
  "secret": "your-secret",
  "service_type_id": "123456",
  "search_mode": "service_type"
}
```

### PCO Endpoints

#### `POST /api/test-pco`
Test PCO credentials without saving.

**Request:**
```json
{
  "app_id": "your-app-id",
  "secret": "your-secret"
}
```

**Response:**
```json
{
  "success": true,
  "service_types": [
    {"id": "123456", "name": "Sunday Service", "frequency": "Weekly"}
  ]
}
```

#### `GET /api/service-types`
List available PCO service types (requires saved credentials).

### Control Endpoints

#### `POST /api/restart/video`
Restart mpv process only.

#### `POST /api/restart/overlay`
Restart PCO overlay updater only.

#### `POST /api/restart/all`
Restart both mpv and overlay.

#### `POST /api/reboot`
Reboot the entire Raspberry Pi.

#### `POST /api/shutdown`
Shut down the Raspberry Pi. Requires physical access to power it back on.

### Configuration Backup Endpoints

#### `GET /api/config/export`
Download the current configuration as a TOML file. Sensitive fields (`pco.secret`) are stripped.

**Response:** `application/toml` file download

#### `POST /api/config/import`
Upload a TOML configuration file. Validates and merges settings, preserving existing secrets.

**Request:** `multipart/form-data` with `file` field containing `.toml` file

### WebSocket Endpoints

#### `WS /ws/status`
Real-time status updates (JSON, every 2 seconds).

#### `WS /ws/preview`
Live video preview frames (JPEG binary, every 2 seconds).

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

### `[display]` Section

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `hide_cursor` | bool | `true` | Hide mouse cursor |

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

[display]
hide_cursor = true

[web]
port = 80

[network]
hotspot_ssid = "Sanctuary-Setup"
hotspot_password = "mypassword"
ethernet_timeout = 10
wifi_timeout = 40
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
- `unclutter` - Cursor hiding (optional)

---

## Bitfocus Companion Integration

The pi-decoder exposes stream state via the `/api/status` endpoint, which can be polled by Bitfocus Companion to show stream health indicators.

### Key Status Fields

| Field | Value | Meaning |
|-------|-------|---------|
| `mpv.idle` | `true` | No stream playing (encoder down) |
| `mpv.idle` | `false` | Stream is active |
| `mpv.playing` | `true` | Actively playing video |
| `mpv.alive` | `true` | mpv process is running |
| `mpv.alive` | `false` | mpv process crashed |

### Example Companion Setup

1. Create HTTP GET request to `http://<PI-IP>/api/status`
2. Poll every 2-5 seconds
3. Parse JSON and check `mpv.idle` field
4. Set button color: RED if idle, GREEN if playing

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
