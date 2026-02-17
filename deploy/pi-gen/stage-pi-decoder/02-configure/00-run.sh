#!/bin/bash -e

# Install systemd service
install -m 644 "${STAGE_DIR}/02-configure/files/pi-decoder.service" \
    "${ROOTFS_DIR}/etc/systemd/system/"

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

# Install network fallback script and service
install -d "${ROOTFS_DIR}/opt/pi-decoder/deploy"
install -m 755 "${STAGE_DIR}/02-configure/files/pi-decoder-network.sh" \
    "${ROOTFS_DIR}/opt/pi-decoder/deploy/"
install -m 644 "${STAGE_DIR}/02-configure/files/pi-decoder-network.service" \
    "${ROOTFS_DIR}/etc/systemd/system/"

# Sudoers for pi user (service restart and reboot without password)
install -m 440 "${STAGE_DIR}/02-configure/files/sudoers-pi-decoder" \
    "${ROOTFS_DIR}/etc/sudoers.d/pi-decoder"

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
