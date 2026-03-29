#!/bin/bash -e

# Install systemd service (substitute user/group for non-pi usernames)
sed "s|^User=.*|User=${FIRST_USER_NAME}|;s|^Group=.*|Group=${FIRST_USER_NAME}|" \
    "${STAGE_DIR}/02-configure/files/pi-decoder.service" \
    > "${ROOTFS_DIR}/etc/systemd/system/pi-decoder.service"
chmod 644 "${ROOTFS_DIR}/etc/systemd/system/pi-decoder.service"

# Create config directory and default config
install -d -m 755 "${ROOTFS_DIR}/etc/pi-decoder"
install -m 600 "${STAGE_DIR}/02-configure/files/config.toml.default" \
    "${ROOTFS_DIR}/etc/pi-decoder/config.toml"

# Configure unattended upgrades
install -m 644 "${STAGE_DIR}/02-configure/files/50unattended-upgrades" \
    "${ROOTFS_DIR}/etc/apt/apt.conf.d/"
install -m 644 "${STAGE_DIR}/02-configure/files/20auto-upgrades" \
    "${ROOTFS_DIR}/etc/apt/apt.conf.d/"

# Install single-interface policy dispatcher script
install -m 755 "${STAGE_DIR}/02-configure/files/10-pi-decoder-single-iface" \
    "${ROOTFS_DIR}/etc/NetworkManager/dispatcher.d/"

# Install dnsmasq config for captive portal DNS (resolves all queries to hotspot IP)
install -d "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d"
install -m 644 "${STAGE_DIR}/02-configure/files/captive-portal-dnsmasq.conf" \
    "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/"

# Install network fallback script and service
install -d "${ROOTFS_DIR}/opt/pi-decoder/deploy"
install -m 755 "${STAGE_DIR}/02-configure/files/pi-decoder-network.sh" \
    "${ROOTFS_DIR}/opt/pi-decoder/deploy/"
install -m 644 "${STAGE_DIR}/02-configure/files/pi-decoder-network.service" \
    "${ROOTFS_DIR}/etc/systemd/system/"

# Sudoers for service user (service restart and reboot without password)
sed "s|^pi |${FIRST_USER_NAME} |g" \
    "${STAGE_DIR}/02-configure/files/sudoers-pi-decoder" \
    > "${ROOTFS_DIR}/etc/sudoers.d/pi-decoder"
chmod 440 "${ROOTFS_DIR}/etc/sudoers.d/pi-decoder"

# Config ownership and group membership
on_chroot << EOF
chown -R "${FIRST_USER_NAME}:${FIRST_USER_NAME}" /etc/pi-decoder
usermod -aG systemd-journal,video,render "${FIRST_USER_NAME}"
EOF

# Journald log limits
install -d "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
install -m 644 "${STAGE_DIR}/02-configure/files/journald-pi-decoder.conf" \
    "${ROOTFS_DIR}/etc/systemd/journald.conf.d/pi-decoder.conf"

# Set hostname to 'pi-decoder' for mDNS (pi-decoder.local)
echo "pi-decoder" > "${ROOTFS_DIR}/etc/hostname"
sed -i 's/127\.0\.1\.1.*/127.0.1.1\tpi-decoder/' "${ROOTFS_DIR}/etc/hosts"

# Enable services
on_chroot << EOF
systemctl enable pi-decoder
systemctl enable pi-decoder-network
systemctl enable NetworkManager-wait-online.service
systemctl enable avahi-daemon
systemctl disable dphys-swapfile || true
EOF

# CLI autologin (no desktop — mpv renders directly via DRM)
on_chroot << EOF
raspi-config nonint do_boot_behaviour B2
EOF

# ── Read-only root filesystem ────────────────────────────────────────

# Switch journal to volatile (tmpfs) — no disk writes for logs
cat > "${ROOTFS_DIR}/etc/systemd/journald.conf.d/pi-decoder.conf" << 'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=30M
MaxRetentionSec=1month
EOF

# Add tmpfs entries for writable directories
for entry in \
    "tmpfs /var/log tmpfs nodev,nosuid,size=30M 0 0" \
    "tmpfs /var/tmp tmpfs nodev,nosuid,size=10M 0 0" \
    "tmpfs /var/lib/systemd tmpfs nodev,nosuid,size=5M 0 0" \
    "tmpfs /tmp tmpfs nodev,nosuid,size=50M 0 0"; do
    mp=$(echo "$entry" | awk '{print $2}')
    if ! grep -q "tmpfs\s\+${mp}\s" "${ROOTFS_DIR}/etc/fstab"; then
        echo "$entry" >> "${ROOTFS_DIR}/etc/fstab"
    fi
done

# Add 'ro' to root ext4 mount
if grep -q '^\S\+\s\+/\s\+ext4' "${ROOTFS_DIR}/etc/fstab"; then
    if ! grep -q '^\S\+\s\+/\s\+ext4\s\+.*\bro\b' "${ROOTFS_DIR}/etc/fstab"; then
        sed -i 's|^\(\S\+\s\+/\s\+ext4\s\+\)\(\S\+\)|\1\2,ro|' "${ROOTFS_DIR}/etc/fstab"
    fi
fi

# Add 'ro' to /boot/firmware vfat mount
if grep -q '^\S\+\s\+/boot/firmware\s\+vfat' "${ROOTFS_DIR}/etc/fstab"; then
    if ! grep -q '^\S\+\s\+/boot/firmware\s\+vfat\s\+.*\bro\b' "${ROOTFS_DIR}/etc/fstab"; then
        sed -i 's|^\(\S\+\s\+/boot/firmware\s\+vfat\s\+\)\(\S\+\)|\1\2,ro|' "${ROOTFS_DIR}/etc/fstab"
    fi
fi

# Install dpkg pre/post hooks so unattended-upgrades can still install
cat > "${ROOTFS_DIR}/etc/apt/apt.conf.d/01-remount-rw" << 'EOF'
// Remount root rw before dpkg, ro after — allows unattended-upgrades on ro root
DPkg::Pre-Invoke  { "mount -o remount,rw / 2>/dev/null || true"; };
DPkg::Post-Invoke { "sync && mount -o remount,ro / 2>/dev/null || true"; };
EOF
