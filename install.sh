#!/bin/bash

set -euo pipefail

# ======================================
#         ROOT CHECK
# ======================================

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root"
    exit 1
fi

# ======================================
#          PATH CONFIG
# ======================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DIR="/opt/nasberry"
APP_PATH="$INSTALL_DIR/nasberrypi.py"

BIN_PATH="/usr/local/bin/nasberry"

MOUNT_POINT="${NASBERRY_MOUNT_POINT:-/mnt/nasberry}"

# ======================================
#           INSTALL START
# ======================================

clear

echo "======================================"
echo " Installing Nasberry NAS System..."
echo "======================================"

# ======================================
#         PACKAGE INSTALL
# ======================================

apt-get update

apt-get install -y \
    python3 \
    python3-pip \
    samba \
    cifs-utils \
    net-tools

# ======================================
#        INSTALL DIRECTORY
# ======================================

install -d -m 755 "$INSTALL_DIR"

# ======================================
#         INSTALL SCRIPT
# ======================================

install -m 755 \
    "$SCRIPT_DIR/nasberrypi.py" \
    "$APP_PATH"

# ======================================
#          MOUNT POINT
# ======================================

mkdir -p "$MOUNT_POINT"

chmod 755 "$MOUNT_POINT"

# ======================================
#         CREATE COMMAND
# ======================================

ln -sf "$APP_PATH" "$BIN_PATH"

# ======================================
#        INSTALL COMPLETE
# ======================================

clear

echo "======================================"
echo " Nasberry installation complete!"
echo ""
echo " Installed to:"
echo "   $APP_PATH"
echo ""
echo " Launch using:"
echo "   nasberry"
echo ""
echo " Recommended before real NAS use:"
echo "   export NASBERRY_PIN='your-new-pin'"
echo "======================================"
