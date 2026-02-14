#!/bin/bash -e

# Copy application to /opt
install -d "${ROOTFS_DIR}/opt/pi-decoder"
cp -r "${STAGE_DIR}/01-install-app/files/pi-decoder/"* "${ROOTFS_DIR}/opt/pi-decoder/"

# Install Python package in venv
on_chroot << EOF
python3 -m venv /opt/pi-decoder/venv
/opt/pi-decoder/venv/bin/pip install /opt/pi-decoder
chown -R "${FIRST_USER_NAME}:${FIRST_USER_NAME}" /opt/pi-decoder
EOF
