#!/bin/bash
# Pi-Decoder — Disable Read-Only Root Filesystem
# Reverts fstab changes for maintenance/debugging.
set -e

GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${GREEN}Disabling read-only root filesystem...${NC}"

# Remount rw first so we can edit fstab
mount -o remount,rw / 2>/dev/null || true

FSTAB="/etc/fstab"

# ── 1. Remove 'ro' from root ext4 mount ─────────────────────────────
if grep -q '^\S\+\s\+/\s\+ext4\s\+.*\bro\b' "$FSTAB"; then
    sed -i 's|^\(\S\+\s\+/\s\+ext4\s\+\S*\),ro|\1|' "$FSTAB"
    # Also handle case where ro is at the start of options
    sed -i 's|^\(\S\+\s\+/\s\+ext4\s\+\)ro,|\1|' "$FSTAB"
    sed -i 's|^\(\S\+\s\+/\s\+ext4\s\+\)ro\s|\1defaults |' "$FSTAB"
    echo "  Removed 'ro' from root mount"
fi

# ── 2. Remove 'ro' from /boot/firmware vfat mount ───────────────────
if grep -q '^\S\+\s\+/boot/firmware\s\+vfat\s\+.*\bro\b' "$FSTAB"; then
    sed -i 's|^\(\S\+\s\+/boot/firmware\s\+vfat\s\+\S*\),ro|\1|' "$FSTAB"
    sed -i 's|^\(\S\+\s\+/boot/firmware\s\+vfat\s\+\)ro,|\1|' "$FSTAB"
    sed -i 's|^\(\S\+\s\+/boot/firmware\s\+vfat\s\+\)ro\s|\1defaults |' "$FSTAB"
    echo "  Removed 'ro' from /boot/firmware mount"
fi

# ── 3. Remove tmpfs entries ──────────────────────────────────────────
for mp in /var/log /var/tmp /var/lib/systemd; do
    if grep -q "tmpfs\s\+${mp}\s" "$FSTAB"; then
        sed -i "\|tmpfs\s\+${mp}\s|d" "$FSTAB"
        echo "  Removed tmpfs for ${mp}"
    fi
done

# ── 4. Remove dpkg remount hooks ────────────────────────────────────
APT_HOOK="/etc/apt/apt.conf.d/01-remount-rw"
if [ -f "$APT_HOOK" ]; then
    rm "$APT_HOOK"
    echo "  Removed dpkg remount hooks"
fi

# ── 5. Switch journal back to persistent ─────────────────────────────
JOURNAL_CONF="/etc/systemd/journald.conf.d/decoder.conf"
if [ -f "$JOURNAL_CONF" ]; then
    cat > "$JOURNAL_CONF" << 'EOF'
[Journal]
SystemMaxUse=50M
MaxRetentionSec=1month
EOF
    echo "  Switched journal back to persistent storage"
    systemctl restart systemd-journald 2>/dev/null || true
fi

sync

echo -e "${GREEN}Read-only root disabled. Reboot to activate.${NC}"
