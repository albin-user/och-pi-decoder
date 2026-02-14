#!/bin/bash -e

# Add HDMI settings to config.txt
cat >> "${ROOTFS_DIR}/boot/firmware/config.txt" << 'EOF'

# Pi-Decoder HDMI settings
hdmi_force_hotplug=1
hdmi_group=1
hdmi_mode=16
hdmi_drive=2
disable_overscan=1
EOF
