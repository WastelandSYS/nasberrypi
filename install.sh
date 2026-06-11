#!/bin/bash
set -euo pipefail

INSTALL_DIR="/opt/nasberry"
APP_PATH="$INSTALL_DIR/nasberrypi.py"
BIN_PATH="/usr/local/bin/nasberry"
MOUNT_POINT="${NASBERRY_MOUNT_POINT:-/mnt/nasberry}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '%s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ "$EUID" -eq 0 ] || die "Run this installer as root: sudo ./install.sh"

if [ "${1:-}" = "--uninstall" ]; then
    shift
    if [ -x "$SCRIPT_DIR/uninstall.sh" ]; then
        exec "$SCRIPT_DIR/uninstall.sh" "$@"
    elif [ -x "$INSTALL_DIR/uninstall.sh" ]; then
        exec "$INSTALL_DIR/uninstall.sh" "$@"
    fi
    die "uninstall.sh was not found; download it beside install.sh and try again"
fi

command -v apt-get >/dev/null || die "This installer currently supports Debian, Ubuntu, and Raspberry Pi OS (apt-get required)."
[ -f "$SCRIPT_DIR/nasberrypi.py" ] || die "nasberrypi.py was not found beside install.sh"
[ -f "$SCRIPT_DIR/uninstall.sh" ] || die "uninstall.sh was not found beside install.sh"

log "Installing Nasberry dependencies..."
apt-get update
apt-get install -y python3 samba cifs-utils util-linux iproute2

install -d -m 755 "$INSTALL_DIR" "$MOUNT_POINT"
install -m 755 "$SCRIPT_DIR/nasberrypi.py" "$APP_PATH"
install -m 755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
ln -sf "$APP_PATH" "$BIN_PATH"
hash -r

log ""
log "Nasberry installation complete."
log "  Application: $APP_PATH"
log "  Command:     $BIN_PATH"
log "  Mount point: $MOUNT_POINT"
log "  Version:     $("$BIN_PATH" --version)"
log "  Source:      $SCRIPT_DIR"
log ""
log "Next step: run 'sudo nasberry setup' to select a drive, create a PIN, and configure Samba."
