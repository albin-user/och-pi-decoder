#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

echo "========================================"
echo "  Pi-Decoder - Pi Image Builder"
echo "========================================"
echo ""

# Clone pi-gen if not present
if [ ! -d "$SCRIPT_DIR/pi-gen" ]; then
    echo "Cloning pi-gen..."
    git clone --depth 1 https://github.com/RPi-Distro/pi-gen.git "$SCRIPT_DIR/pi-gen"
fi

# Copy our custom stage
echo "Copying custom stage..."
rm -rf "$SCRIPT_DIR/pi-gen/stage-pi-decoder"
cp -r "$SCRIPT_DIR/stage-pi-decoder" "$SCRIPT_DIR/pi-gen/"

# Copy the application source
echo "Copying application source..."
mkdir -p "$SCRIPT_DIR/pi-gen/stage-pi-decoder/01-install-app/files/pi-decoder"
cp -r "$PROJECT_DIR/src" "$SCRIPT_DIR/pi-gen/stage-pi-decoder/01-install-app/files/pi-decoder/"
cp "$PROJECT_DIR/pyproject.toml" "$SCRIPT_DIR/pi-gen/stage-pi-decoder/01-install-app/files/pi-decoder/"
cp "$PROJECT_DIR/README.md" "$SCRIPT_DIR/pi-gen/stage-pi-decoder/01-install-app/files/pi-decoder/" 2>/dev/null || true

# Copy config files from deploy/
echo "Copying config files..."
cp "$PROJECT_DIR/deploy/pi-decoder.service" \
   "$SCRIPT_DIR/pi-gen/stage-pi-decoder/02-configure/files/"
cp "$PROJECT_DIR/deploy/config.toml.example" \
   "$SCRIPT_DIR/pi-gen/stage-pi-decoder/02-configure/files/config.toml.default"
cp "$PROJECT_DIR/deploy/pi-decoder-network.sh" \
   "$SCRIPT_DIR/pi-gen/stage-pi-decoder/02-configure/files/"
cp "$PROJECT_DIR/deploy/pi-decoder-network.service" \
   "$SCRIPT_DIR/pi-gen/stage-pi-decoder/02-configure/files/"
cp "$PROJECT_DIR/deploy/journald-pi-decoder.conf" \
   "$SCRIPT_DIR/pi-gen/stage-pi-decoder/02-configure/files/"
cp "$PROJECT_DIR/deploy/sudoers-pi-decoder" \
   "$SCRIPT_DIR/pi-gen/stage-pi-decoder/02-configure/files/"

# Copy our config
cp "$SCRIPT_DIR/config" "$SCRIPT_DIR/pi-gen/"

# Build
echo ""
echo "Starting pi-gen build..."
echo ""
cd "$SCRIPT_DIR/pi-gen"
sudo ./build.sh

echo ""
echo "========================================"
echo "  Build complete!"
echo "========================================"
echo ""
echo "Image is in: $SCRIPT_DIR/pi-gen/deploy/"
echo ""
