#!/bin/bash
# Pi-Decoder — Enable Read-Only Root Filesystem
# Idempotent: safe to run multiple times.
# Prevents SD card corruption from power loss.
set -e

GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${GREEN}Enabling read-only root filesystem...${NC}"

FSTAB="/etc/fstab"

# ── 1. Add 'ro' to root ext4 mount ──────────────────────────────────
if grep -q '^\S\+\s\+/\s\+ext4' "$FSTAB"; then
    if ! grep -q '^\S\+\s\+/\s\+ext4\s\+.*\bro\b' "$FSTAB"; then
        sed -i 's|^\(\S\+\s\+/\s\+ext4\s\+\)\(\S\+\)|\1\2,ro|' "$FSTAB"
        echo "  Added 'ro' to root mount in fstab"
    else
        echo "  Root mount already has 'ro'"
    fi
fi

# ── 2. Add 'ro' to /boot/firmware vfat mount ────────────────────────
if grep -q '^\S\+\s\+/boot/firmware\s\+vfat' "$FSTAB"; then
    if ! grep -q '^\S\+\s\+/boot/firmware\s\+vfat\s\+.*\bro\b' "$FSTAB"; then
        sed -i 's|^\(\S\+\s\+/boot/firmware\s\+vfat\s\+\)\(\S\+\)|\1\2,ro|' "$FSTAB"
        echo "  Added 'ro' to /boot/firmware mount in fstab"
    else
        echo "  /boot/firmware mount already has 'ro'"
    fi
fi

# ── 3. Add tmpfs entries for writable directories ────────────────────
add_tmpfs() {
    local mp="$1" size="$2"
    if ! grep -q "tmpfs\s\+${mp}\s" "$FSTAB"; then
        echo "tmpfs ${mp} tmpfs nodev,nosuid,size=${size} 0 0" >> "$FSTAB"
        echo "  Added tmpfs for ${mp} (${size})"
    else
        echo "  tmpfs for ${mp} already exists"
    fi
}

add_tmpfs /var/log 30M
add_tmpfs /var/tmp 10M
add_tmpfs /var/lib/systemd 5M

# ── 4. dpkg hooks for unattended-upgrades ────────────────────────────
APT_HOOK="/etc/apt/apt.conf.d/01-remount-rw"
if [ ! -f "$APT_HOOK" ]; then
    cat > "$APT_HOOK" << 'EOF'
// Remount root rw before dpkg, ro after — allows unattended-upgrades on ro root
DPkg::Pre-Invoke  { "mount -o remount,rw / 2>/dev/null || true"; };
DPkg::Post-Invoke { "sync && mount -o remount,ro / 2>/dev/null || true"; };
EOF
    echo "  Installed dpkg remount hooks"
else
    echo "  dpkg remount hooks already installed"
fi

echo -e "${GREEN}Read-only root enabled. Reboot to activate.${NC}"
