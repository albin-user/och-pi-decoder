# Decoder - Custom Pi OS Image

Build a custom Raspberry Pi OS image with the Decoder pre-installed.

## What This Does

Creates a complete `.img` file that you can flash to an SD card. When booted:
- Decoder is pre-installed and configured
- CLI autologin (no desktop environment)
- DRM display output configured (mpv renders directly to HDMI)
- SSH enabled for remote access
- Security updates configured

## Prerequisites

### On Debian/Ubuntu (Native Build)

```bash
sudo apt install coreutils quilt parted qemu-user-static debootstrap \
    zerofree zip dosfstools libarchive-tools libcap2-bin grep rsync \
    xz-utils file git curl bc qemu-utils kpartx gpg pigz
```

### On macOS/Windows (Docker Build)

Docker must be installed and running. The build will use `build-docker.sh` instead.

## Building

```bash
cd deploy/pi-gen
./build.sh
```

### Build Time

- **First build:** 30-60 minutes (downloads base system)
- **Incremental builds:** ~10 minutes

### Output

The finished image will be at:
```
deploy/pi-gen/pi-gen/deploy/image_YYYY-MM-DD-decoder.img.xz
```

## Flashing the Image

### Using Raspberry Pi Imager

1. Open Raspberry Pi Imager
2. Choose OS → Use custom → Select the `.img.xz` file
3. Choose your SD card
4. Click Write

### Using dd (Linux/macOS)

```bash
xz -d image_*.img.xz
sudo dd if=image_*.img of=/dev/sdX bs=4M status=progress
```

## Default Credentials

- **Username:** pi
- **Password:** raspberry
- **SSH:** Enabled

Change the password after first boot!

## Configuration

After flashing and booting, configure your stream and PCO settings:

```bash
sudo nano /etc/decoder/config.toml
sudo systemctl restart decoder
```

Or use the web interface at `http://<pi-ip-address>/`

## Docker Build (macOS/Windows)

If building on macOS or Windows, use Docker:

```bash
cd deploy/pi-gen
./build.sh  # Will clone pi-gen first

# Then run the Docker build
cd pi-gen
./build-docker.sh
```

## What APT Updates Still Work

The custom image uses official Raspberry Pi OS repositories. Running:

```bash
sudo apt update && sudo apt upgrade
```

...updates all system packages normally. The decoder app itself is not from apt, so manual updates are required for that.

## Customizing the Build

### Change default timezone/locale

Edit `deploy/pi-gen/config`:

```bash
TIMEZONE_DEFAULT="America/New_York"
LOCALE_DEFAULT="en_US.UTF-8"
```

### Change default hostname

Edit `deploy/pi-gen/config`:

```bash
TARGET_HOSTNAME="sanctuary-decoder"
```

### Add more packages

Edit `deploy/pi-gen/stage-decoder/00-install-packages/00-packages`

## Troubleshooting

### Build fails with permission error

Make sure you're running with sudo or as root.

### Out of disk space

pi-gen needs ~10GB free space for the build.

### QEMU errors

Ensure qemu-user-static is installed for ARM emulation.

## Directory Structure

```
deploy/pi-gen/
├── config                      # Build configuration
├── build.sh                    # Build wrapper script
├── README.md                   # This file
└── stage-decoder/              # Custom pi-gen stage
    ├── DEPENDS
    ├── 00-install-packages/
    │   └── 00-packages
    ├── 01-install-app/
    │   └── 00-run.sh
    ├── 02-configure/
    │   ├── 00-run.sh
    │   └── files/
    └── 03-boot-config/
        └── 00-run.sh
```
