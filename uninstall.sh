#!/bin/bash
set -euo pipefail

INSTALL_DIR="${NASBERRY_INSTALL_DIR:-/opt/nasberry}"
BIN_PATH="${NASBERRY_BIN_PATH:-/usr/local/bin/nasberry}"
SYSTEM_BIN_PATH="${NASBERRY_SYSTEM_BIN_PATH:-/usr/bin/nasberry}"
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
require_command() { command -v "$1" >/dev/null || die "Required command '$1' was not found."; }
safe_removal_path() {
    case "$1" in ""|/|.) die "Refusing unsafe removal path: ${1:-<empty>}" ;; esac
}
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
require_command mountpoint
safe_removal_path "$INSTALL_DIR"
safe_removal_path "$BIN_PATH"
safe_removal_path "$SYSTEM_BIN_PATH"
safe_removal_path "$CONFIG_DIR"
safe_removal_path "$MOUNT_POINT"
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
    local appliance_header="# Managed by Nasberry appliance mode. Previous config is saved before replacement."
    if ! grep -Fq "$marker" "$SMB_CONF" && ! grep -Fq "# BEGIN Managed by Nasberry appliance mode" "$SMB_CONF" && ! grep -Fq "$appliance_header" "$SMB_CONF"; then
        log "No Nasberry-managed Samba settings found; leaving Samba configuration unchanged."
        return 0
    fi
    if "$DRY_RUN"; then
        log "Would remove Nasberry-managed Samba settings from $SMB_CONF after backup and validation."
        return 0
    fi
    for required_command in python3 testparm install stat mktemp cp; do
        require_command "$required_command"
    done

    local backup temp
    backup="${SMB_CONF}.nasberry-uninstall.$(date +%Y%m%d%H%M%S%N).bak"
    temp="$(mktemp "${SMB_CONF}.nasberry-uninstall.XXXXXX")"
    cp -a "$SMB_CONF" "$backup"
    python3 - "$SMB_CONF" "$temp" "$marker" "$appliance_header" "$SHARE_NAME" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
marker = sys.argv[3]
appliance_header = sys.argv[4]
share_header = f"[{sys.argv[5]}]".lower()
lines = source.read_text().splitlines(keepends=True)
current_appliance = any(line.strip() == appliance_header for line in lines)
output = []
in_managed_block = False
skip_section = False
skip_disabled_setting = False
for line in lines:
    stripped = line.strip()
    if stripped == "# BEGIN Managed by Nasberry appliance mode":
        in_managed_block = True
        continue
    if stripped == "# END Managed by Nasberry appliance mode":
        in_managed_block = False
        continue
    if in_managed_block:
        continue
    if skip_disabled_setting:
        skip_disabled_setting = False
        continue
    if stripped == "# Nasberry appliance mode: disable share":
        skip_disabled_setting = True
        continue
    if stripped in {appliance_header, "# Managed by Nasberry appliance mode", "usershare max shares = 0"}:
        continue
    if stripped == marker or (current_appliance and stripped.lower() == share_header):
        skip_section = True
        continue
    if skip_section:
        if line.lstrip().startswith("["):
            skip_section = False
        else:
            continue
    output.append(line)
destination.write_text("".join(output))
PY
    if ! testparm -s "$temp" >/dev/null 2>&1; then
        rm -f "$temp"
        die "Samba validation failed; live configuration was not changed. Backup: $backup"
    fi
    install -m "$(stat -c '%a' "$SMB_CONF")" "$temp" "$SMB_CONF"
    rm -f "$temp"
    log "Removed Nasberry-managed Samba share. Backup: $backup"
    if command -v systemctl >/dev/null && systemctl is-active --quiet smbd 2>/dev/null; then
        systemctl reload smbd || log "WARNING: Could not reload smbd; reload it manually."
    fi
}

log "Nasberry uninstall plan:"
log "  Remove application: $INSTALL_DIR, $BIN_PATH, and $SYSTEM_BIN_PATH"
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
# Only remove the compatibility path when it is Nasberry's link, so an older
# installation cannot accidentally remove an unrelated /usr/bin command.
if [ -L "$SYSTEM_BIN_PATH" ] && [ "$(readlink "$SYSTEM_BIN_PATH")" = "$INSTALL_DIR/nasberrypi.py" ]; then
    run_action rm -f -- "$SYSTEM_BIN_PATH"
fi
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
