#!/bin/bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/nasberry"
BIN_PATH="/usr/local/bin/nasberry"
APP_PATH="$INSTALL_DIR/nasberrypi.py"
MOUNT_POINT="${NASBERRY_MOUNT_POINT:-/mnt/nasberry}"

clear

echo "Installing Nasberry NAS System..."

apt-get update

apt-get install -y \
    python3 \
    python3-pip \
    samba \
    cifs-utils \
    net-tools

install -d -m 755 "$INSTALL_DIR"
install -m 755 "$SCRIPT_DIR/nasberrypi.py" "$APP_PATH"

mkdir -p "$MOUNT_POINT"
chmod 755 "$MOUNT_POINT"

ln -sf "$APP_PATH" "$BIN_PATH"

clear

echo "======================================"
echo " Nasberry installation complete!"
echo " Launch using: nasberry"
echo ""
echo " Recommended before real NAS use:"
echo "   export NASBERRY_PIN='your-new-pin'"
echo "======================================"
