#!/bin/bash
# Pi-Decoder — Setup Script
# Run with: sudo ./setup.sh
# Target: Raspberry Pi OS Bookworm (64-bit)

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Pi-Decoder — Installer                       ${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"

# ── 1. Root check ────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root: sudo ./setup.sh${NC}"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="${INSTALL_DIR:-/opt/pi-decoder}"
CONFIG_DIR="${CONFIG_DIR:-/etc/pi-decoder}"
SERVICE_USER="${SERVICE_USER:-pi}"

# ── 2. System packages ──────────────────────────────────────────────
echo -e "\n${GREEN}[1/7] Installing system packages...${NC}"

apt update
apt install -y mpv python3-pip cec-utils avahi-daemon yt-dlp

# ── 3. Install Python package (venv) ─────────────────────────────────
echo -e "\n${GREEN}[2/7] Installing Pi-Decoder Python package...${NC}"

# Copy project to /opt
if [ "$PROJECT_DIR" != "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    cp -r "$PROJECT_DIR" "$INSTALL_DIR"
fi

# Create venv and install
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install "$INSTALL_DIR"

# Set ownership
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── 4. Configuration ────────────────────────────────────────────────
echo -e "\n${GREEN}[3/7] Setting up configuration...${NC}"

mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    cp "$SCRIPT_DIR/config.toml.example" "$CONFIG_DIR/config.toml"
    echo "Created default config at $CONFIG_DIR/config.toml"
else
    echo "Config already exists, keeping current settings."
fi

chmod 600 "$CONFIG_DIR/config.toml"
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"

# ── 5. systemd service ──────────────────────────────────────────────
echo -e "\n${GREEN}[4/7] Installing systemd service...${NC}"

cp "$SCRIPT_DIR/pi-decoder.service" /etc/systemd/system/
cp "$SCRIPT_DIR/pi-decoder-network.service" /etc/systemd/system/
chmod +x "$SCRIPT_DIR/pi-decoder-network.sh"
systemctl daemon-reload
systemctl enable pi-decoder
systemctl enable pi-decoder-network

# Install single-interface policy dispatcher script
install -m 755 "$SCRIPT_DIR/10-pi-decoder-single-iface" /etc/NetworkManager/dispatcher.d/

# Remove legacy services if present
for svc in vlc-video pco-overlay config-web vlc-watchdog vlc-process-monitor x0vncserver church-decoder church-decoder-network decoder decoder-network; do
    if systemctl is-enabled "$svc" 2>/dev/null; then
        echo "  Disabling legacy service: $svc"
        systemctl disable "$svc" 2>/dev/null || true
        systemctl stop "$svc" 2>/dev/null || true
    fi
    rm -f "/etc/systemd/system/${svc}.service"
done
systemctl daemon-reload

# Remove legacy autostart
rm -f "/home/$SERVICE_USER/.config/autostart/vlc-kiosk.desktop"

# Sudoers for service user (service restart and reboot without password)
install -m 440 "$SCRIPT_DIR/sudoers-pi-decoder" /etc/sudoers.d/decoder

# Allow service user to read journal logs and access DRM devices
usermod -aG systemd-journal,video,render "$SERVICE_USER" 2>/dev/null || true

# ── 5b. journald log limits ────────────────────────────────────────
mkdir -p /etc/systemd/journald.conf.d
cp "$SCRIPT_DIR/journald-pi-decoder.conf" /etc/systemd/journald.conf.d/decoder.conf
systemctl restart systemd-journald 2>/dev/null || true

# ── 6. Pi boot config ───────────────────────────────────────────────
echo -e "\n${GREEN}[5/7] Configuring Pi boot settings...${NC}"

CONFIG_TXT="/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"

set_boot_config() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$CONFIG_TXT" 2>/dev/null; then
        sed -i "s/^${key}=.*/${key}=${value}/" "$CONFIG_TXT"
    else
        echo "${key}=${value}" >> "$CONFIG_TXT"
    fi
}

# Legacy firmware HDMI settings (Pi 3 and earlier without KMS driver)
set_boot_config "hdmi_force_hotplug" "1"
set_boot_config "hdmi_group" "1"
set_boot_config "hdmi_mode" "16"
set_boot_config "disable_overscan" "1"
set_boot_config "hdmi_drive" "2"

# KMS/DRM resolution forcing (Pi 4/5 on Bookworm ignore legacy hdmi_* settings)
# Append video= kernel parameter to cmdline.txt for 1080p60 on the KMS driver
CMDLINE="/boot/firmware/cmdline.txt"
[ -f "$CMDLINE" ] || CMDLINE="/boot/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    # Remove any existing video= parameter, then append ours
    sed -i 's/ video=[^ ]*//' "$CMDLINE"
    sed -i 's/$/ video=HDMI-A-1:1920x1080@60D/' "$CMDLINE"
    echo "  Set KMS resolution: 1920x1080@60Hz (cmdline.txt)"
    # Prevent console blanking (no desktop to manage DPMS)
    if ! grep -q 'consoleblank=' "$CMDLINE"; then
        sed -i 's/$/ consoleblank=0/' "$CMDLINE"
    fi
fi

# Disable swap
if command -v dphys-swapfile >/dev/null 2>&1; then
    dphys-swapfile swapoff 2>/dev/null || true
    dphys-swapfile uninstall 2>/dev/null || true
    systemctl disable dphys-swapfile 2>/dev/null || true
fi

# CLI autologin (no desktop — mpv renders directly via DRM)
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_boot_behaviour B2 2>/dev/null || true
fi

# ── 7. Unattended security updates (optional, conservative) ─────────
echo -e "\n${GREEN}[6/7] Configuring automatic security updates...${NC}"

apt install -y unattended-upgrades 2>/dev/null || true

# Configure to only install security updates, not regular updates
cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'EOF'
// Only security updates - won't break your system
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Raspbian,codename=${distro_codename},label=Raspbian-Security";
};

// Don't automatically reboot
Unattended-Upgrade::Automatic-Reboot "false";

// Remove unused dependencies
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF

cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

# ── 8. mDNS / hostname ───────────────────────────────────────────────
echo -e "\n${GREEN}[7/7] Setting hostname to 'pi-decoder' (pi-decoder.local)...${NC}"

hostnamectl set-hostname pi-decoder 2>/dev/null || echo "pi-decoder" > /etc/hostname
sed -i 's/127\.0\.1\.1.*/127.0.1.1\tpi-decoder/' /etc/hosts 2>/dev/null || true
systemctl enable avahi-daemon 2>/dev/null || true

# ── Done ─────────────────────────────────────────────────────────────
echo -e "\nEnabling network services..."
systemctl enable NetworkManager-wait-online.service 2>/dev/null || true

IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "  Config file:   $CONFIG_DIR/config.toml"
echo "  Web interface: http://${IP}"
echo "  Service:       sudo systemctl status pi-decoder"
echo ""
echo "  Please reboot to start: sudo reboot"
echo ""
