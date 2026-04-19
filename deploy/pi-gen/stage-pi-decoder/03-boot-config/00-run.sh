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

# KMS/DRM resolution + prevent console blanking
CMDLINE="${ROOTFS_DIR}/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    # NOTE: No 'D' flag — it skips EDID readback and breaks libcec PA detection
    sed -i 's/$/ video=HDMI-A-1:1920x1080@30 consoleblank=0/' "$CMDLINE"
fi
