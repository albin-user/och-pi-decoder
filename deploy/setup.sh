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
apt install -y mpv python3-pip unclutter cec-utils avahi-daemon yt-dlp

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

# Allow service user to read journal logs
usermod -aG systemd-journal "$SERVICE_USER" 2>/dev/null || true

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

set_boot_config "hdmi_force_hotplug" "1"
set_boot_config "hdmi_group" "1"
set_boot_config "hdmi_mode" "16"
set_boot_config "disable_overscan" "1"
set_boot_config "hdmi_drive" "2"
# gpu_mem removed - Pi 5 ignores this setting (uses dynamic GPU memory allocation)

# Disable swap
if command -v dphys-swapfile >/dev/null 2>&1; then
    dphys-swapfile swapoff 2>/dev/null || true
    dphys-swapfile uninstall 2>/dev/null || true
    systemctl disable dphys-swapfile 2>/dev/null || true
fi

# Desktop autologin
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_boot_behaviour B4 2>/dev/null || true
fi

# ── 7. Kiosk mode hardening ────────────────────────────────────────
echo -e "\n${GREEN}[6/9] Configuring kiosk mode (cursor, popups, notifications)...${NC}"

AUTOSTART_DIR="/home/$SERVICE_USER/.config/autostart"
LXSESSION_DIR="/home/$SERVICE_USER/.config/lxsession/LXDE-pi"
LXPANEL_DIR="/home/$SERVICE_USER/.config/lxpanel/LXDE-pi/panels"
PCMANFM_DIR="/home/$SERVICE_USER/.config/pcmanfm/LXDE-pi"
mkdir -p "$AUTOSTART_DIR" "$LXSESSION_DIR" "$LXPANEL_DIR" "$PCMANFM_DIR"

# Hide cursor
cat > "$AUTOSTART_DIR/unclutter.desktop" << 'EOF'
[Desktop Entry]
Type=Application
Name=Hide Cursor
Exec=unclutter -idle 0.1 -root
X-GNOME-Autostart-enabled=true
EOF

# Disable screen blanking/saver
cat > "$AUTOSTART_DIR/disable-screensaver.desktop" << 'EOF'
[Desktop Entry]
Type=Application
Name=Disable Screen Blanking
Exec=sh -c "xset s off 2>/dev/null; xset s noblank 2>/dev/null; xset -dpms 2>/dev/null || true"
X-GNOME-Autostart-enabled=true
EOF

# Disable PCManFM desktop (prevents desktop icons and right-click menu)
cat > "$PCMANFM_DIR/desktop-items-0.conf" << 'EOF'
[*]
wallpaper_mode=color
wallpaper_common=1
desktop_bg=#000000
desktop_fg=#ffffff
desktop_shadow=#000000
show_documents=0
show_trash=0
show_mounts=0
EOF

# Disable welcome wizard (piwiz) if it exists
rm -f "$AUTOSTART_DIR/piwiz.desktop" 2>/dev/null || true
if [ -f /etc/xdg/autostart/piwiz.desktop ]; then
    mkdir -p /etc/xdg/autostart-disabled
    mv /etc/xdg/autostart/piwiz.desktop /etc/xdg/autostart-disabled/ 2>/dev/null || true
fi

# Disable update notifier popup
for notifier in update-notifier pk-update-icon; do
    rm -f "$AUTOSTART_DIR/${notifier}.desktop" 2>/dev/null || true
    if [ -f "/etc/xdg/autostart/${notifier}.desktop" ]; then
        mkdir -p /etc/xdg/autostart-disabled
        mv "/etc/xdg/autostart/${notifier}.desktop" /etc/xdg/autostart-disabled/ 2>/dev/null || true
    fi
done

# Disable lxplug-* notification plugins
for plugin in lxplug-bluetooth lxplug-ejecter lxplug-network lxplug-volume lxplug-updater; do
    rm -f "$AUTOSTART_DIR/${plugin}.desktop" 2>/dev/null || true
done

# Disable Bluetooth applet autostart
rm -f "$AUTOSTART_DIR/blueman.desktop" 2>/dev/null || true

# Disable print applet
rm -f "$AUTOSTART_DIR/print-applet.desktop" 2>/dev/null || true

# Configure LXDE to not show desktop panel (optional - comment out if you want panel)
# This creates an empty panel config to prevent the taskbar
cat > "$LXPANEL_DIR/panel" << 'EOF'
Global {
  edge=none
  autohide=1
  heighttype=percent
  height=0
}
EOF

# Disable LXDE session manager dialogs
cat > "$LXSESSION_DIR/desktop.conf" << 'EOF'
[Session]
window_manager=openbox-lxde-pi
disable_autostart=no
polkit/command=lxpolkit
clipboard/command=lxclipboard
xsettings_manager/command=build-in

[GTK]
iNet/DoubleClickTime=400
iNet/DoubleClickDistance=5
iNet/DndDragThreshold=8
iNet/CursorBlinkTime=1200
iGtk/CanChangeAccels=0
iGtk/ToolbarStyle=3
iGtk/MenuImages=1
iGtk/ButtonImages=1
iXft/Antialias=1
sNet/ThemeName=PiXflat
sNet/IconThemeName=PiXflat
sGtk/FontName=PibotoLt 12
iGtk/ToolbarIconSize=3
sGtk/ColorScheme=
EOF

# Disable power management dialogs (xfce4-power-manager if present)
if command -v xfce4-power-manager >/dev/null 2>&1; then
    mkdir -p "/home/$SERVICE_USER/.config/xfce4/xfconf/xfce-perchannel-xml"
    cat > "/home/$SERVICE_USER/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="show-tray-icon" type="bool" value="false"/>
    <property name="general-notification" type="bool" value="false"/>
    <property name="dpms-enabled" type="bool" value="false"/>
  </property>
</channel>
EOF
fi

# Set ownership
chown -R "$SERVICE_USER:$SERVICE_USER" "/home/$SERVICE_USER/.config"

# ── 8. Unattended security updates (optional, conservative) ─────────
echo -e "\n${GREEN}[7/9] Configuring automatic security updates...${NC}"

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
echo -e "\n${GREEN}[8/9] Setting hostname to 'pi-decoder' (pi-decoder.local)...${NC}"

hostnamectl set-hostname pi-decoder 2>/dev/null || echo "pi-decoder" > /etc/hostname
sed -i 's/127\.0\.1\.1.*/127.0.1.1\tpi-decoder/' /etc/hosts 2>/dev/null || true
systemctl enable avahi-daemon 2>/dev/null || true

# ── Done ─────────────────────────────────────────────────────────────
echo -e "\n${GREEN}[9/9] Enabling network services...${NC}"
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
