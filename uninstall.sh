#!/bin/bash
set -euo pipefail

INSTALL_DIR="${NASBERRY_INSTALL_DIR:-/opt/nasberry}"
BIN_PATH="${NASBERRY_BIN_PATH:-/usr/local/bin/nasberry}"
CONFIG_DIR="${NASBERRY_CONFIG_DIR:-/etc/nasberry}"
SMB_CONF="${NASBERRY_SMB_CONF:-/etc/samba/smb.conf}"
MOUNT_POINT="${NASBERRY_MOUNT_POINT:-/mnt/nasberry}"
SHARE_NAME="${NASBERRY_SHARE_NAME:-Public}"
PURGE=false
REMOVE_MOUNT_POINT=false
DRY_RUN=false
ASSUME_YES=false

log() { printf '%s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh [options]

Safely removes Nasberry application files. Storage data is never deleted.

Options:
  --purge               Also remove /etc/nasberry and Nasberry's managed Samba share
  --remove-mount-point  Remove the mount-point directory only when it is unmounted and empty
  --dry-run             Show actions without changing anything
  --yes                  Skip the confirmation prompt
  -h, --help             Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --purge) PURGE=true ;;
        --remove-mount-point) REMOVE_MOUNT_POINT=true ;;
        --dry-run) DRY_RUN=true ;;
        --yes) ASSUME_YES=true ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

[ "$EUID" -eq 0 ] || die "Run this uninstaller as root: sudo ./uninstall.sh"

run_action() {
    if "$DRY_RUN"; then
        printf 'Would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

remove_managed_share() {
    [ -f "$SMB_CONF" ] || { log "Samba config not found; skipping managed-share cleanup."; return 0; }
    local marker="# Managed by Nasberry: $SHARE_NAME"
    if ! grep -Fq "$marker" "$SMB_CONF" && ! grep -Fq "# BEGIN Managed by Nasberry appliance mode" "$SMB_CONF"; then
        log "No Nasberry-managed Samba settings found; leaving Samba configuration unchanged."
        return 0
    fi
    if "$DRY_RUN"; then
        log "Would remove Nasberry-managed Samba settings from $SMB_CONF after validation."
        return 0
    fi

    local backup temp
    backup="${SMB_CONF}.nasberry-uninstall.$(date +%Y%m%d%H%M%S).bak"
    temp="$(mktemp)"
    cp -a "$SMB_CONF" "$backup"
    python3 - "$SMB_CONF" "$temp" "$marker" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
marker = sys.argv[3]
lines = source.read_text().splitlines(keepends=True)
output = []
skipping = False
skip_disabled_setting = False
for line in lines:
    stripped = line.strip()
    if stripped == "# Nasberry appliance mode: disable share":
        skip_disabled_setting = True
        continue
    if skip_disabled_setting:
        skip_disabled_setting = False
        continue
    if stripped == "# BEGIN Managed by Nasberry appliance mode":
        skipping = True
        continue
    if stripped == "# END Managed by Nasberry appliance mode":
        skipping = False
        continue
    if stripped in {"# Managed by Nasberry appliance mode", "usershare max shares = 0"}:
        continue
    if stripped == marker:
        skipping = True
        continue
    if skipping and line.lstrip().startswith("["):
        skipping = False
    if not skipping:
        output.append(line)
destination.write_text("".join(output))
PY
    install -m "$(stat -c '%a' "$SMB_CONF")" "$temp" "$SMB_CONF"
    rm -f "$temp"
    if command -v testparm >/dev/null && ! testparm -s "$SMB_CONF" >/dev/null 2>&1; then
        cp -a "$backup" "$SMB_CONF"
        die "Samba validation failed; restored $backup"
    fi
    log "Removed Nasberry-managed Samba share. Backup: $backup"
    if command -v systemctl >/dev/null && systemctl is-active --quiet smbd 2>/dev/null; then
        systemctl reload smbd || log "WARNING: Could not reload smbd; reload it manually."
    fi
}

log "Nasberry uninstall plan:"
log "  Remove application: $INSTALL_DIR and $BIN_PATH"
log "  Purge configuration and managed Samba share: $PURGE"
log "  Remove empty, unmounted mount point: $REMOVE_MOUNT_POINT"
log "  Storage data will never be deleted."

if ! "$ASSUME_YES" && ! "$DRY_RUN"; then
    read -r -p "Continue? [y/N] " answer
    case "$answer" in y|Y|yes|YES) ;; *) log "Cancelled."; exit 0 ;; esac
fi

# Do not stop Samba or unmount storage automatically: both can disrupt unrelated
# shares or active file operations. The user can take Nasberry offline first.
if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    log "WARNING: $MOUNT_POINT is mounted; leaving it and all storage data untouched."
fi

if "$PURGE"; then
    remove_managed_share
    run_action rm -rf -- "$CONFIG_DIR"
else
    log "Preserving configuration in $CONFIG_DIR and Samba configuration."
fi

run_action rm -f -- "$BIN_PATH"
run_action rm -rf -- "$INSTALL_DIR"

if "$REMOVE_MOUNT_POINT"; then
    if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
        log "WARNING: Not removing mounted directory $MOUNT_POINT."
    elif [ -d "$MOUNT_POINT" ] && [ -z "$(find "$MOUNT_POINT" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        run_action rmdir -- "$MOUNT_POINT"
    elif [ -e "$MOUNT_POINT" ]; then
        log "WARNING: Not removing non-empty mount point $MOUNT_POINT."
    fi
fi

log "Nasberry application uninstall complete."
log "Installed packages were preserved because Samba or utilities may be used by other applications."
