# Pi-Decoder Deployment

Deploy the Pi-Decoder to a Raspberry Pi.

## Quick Start

1. Flash **Raspberry Pi OS (64-bit) Desktop** (Bookworm) using Raspberry Pi Imager
2. Copy the project to the Pi
3. Run the setup script:

```bash
cd deploy
sudo ./setup.sh
sudo reboot
```

## Post-Installation

- **Web Interface:** `http://<PI-IP-ADDRESS>/`
- **Config File:** `/etc/pi-decoder/config.toml`
- **Service:** `sudo systemctl status pi-decoder`

See the main documentation for detailed setup and troubleshooting.
