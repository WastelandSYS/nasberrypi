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
    local path="$1"
    local expected_name="${2:-}"
    case "$path" in
        ""|/|.) die "Refusing unsafe removal path: ${path:-<empty>}" ;;
        /*) ;;
        *) die "Refusing non-absolute removal path: $path" ;;
    esac
    case "$path" in
        *"/.."|*"/../"*|*"//"*) die "Refusing unsafe removal path: $path" ;;
    esac
    if [ -n "$expected_name" ] && [ "$(basename "$path")" != "$expected_name" ]; then
        die "Refusing unexpected Nasberry path: $path"
    fi
}
safe_mount_point_path() {
    local path="$1"
    safe_removal_path "$path"
    case "$path" in
        /bin|/boot|/dev|/etc|/home|/lib|/lib64|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/usr/bin|/usr/local|/usr/local/bin|/var)
            die "Refusing unsafe mount-point path: $path"
            ;;
    esac
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
safe_removal_path "$INSTALL_DIR" "nasberry"
safe_removal_path "$BIN_PATH" "nasberry"
safe_removal_path "$SYSTEM_BIN_PATH" "nasberry"
safe_removal_path "$CONFIG_DIR" "nasberry"
safe_mount_point_path "$MOUNT_POINT"
run_action() {
    if "$DRY_RUN"; then
        printf 'Would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

remove_command_link() {
    local path="$1"
    local target="$INSTALL_DIR/nasberrypi.py"
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        return 0
    fi
    if [ -L "$path" ] && [ "$(readlink "$path")" = "$target" ]; then
        run_action rm -f -- "$path"
        return 0
    fi
    log "WARNING: Not removing $path because it is not Nasberry's command link."
}

remove_known_install_file() {
    local path="$1"
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        return 0
    fi
    if [ -d "$path" ] && [ ! -L "$path" ]; then
        log "WARNING: Not removing $path because it is not a Nasberry application file."
        return 0
    fi
    run_action rm -f -- "$path"
}

remove_application_files() {
    if [ ! -e "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ]; then
        return 0
    fi
    if [ -L "$INSTALL_DIR" ]; then
        log "WARNING: Not removing $INSTALL_DIR because it is a symbolic link."
        return 0
    fi
    if [ ! -d "$INSTALL_DIR" ]; then
        log "WARNING: Not removing $INSTALL_DIR because it is not a directory."
        return 0
    fi

    remove_known_install_file "$INSTALL_DIR/nasberrypi.py"
    remove_known_install_file "$INSTALL_DIR/uninstall.sh"

    if "$DRY_RUN"; then
        log "Would remove $INSTALL_DIR if empty after known Nasberry application files are removed."
    elif rmdir -- "$INSTALL_DIR" 2>/dev/null; then
        :
    else
        log "WARNING: Preserving $INSTALL_DIR because it contains files not removed by Nasberry."
    fi
}

remove_managed_share() {
    [ -f "$SMB_CONF" ] || { log "Samba config not found; skipping managed-share cleanup."; return 0; }
    local marker_prefix="# Managed by Nasberry: "
    local appliance_header="# Managed by Nasberry appliance mode. Previous config is saved before replacement."
    local appliance_marker="# Managed by Nasberry appliance mode"
    local appliance_begin="# BEGIN Managed by Nasberry appliance mode"
    local appliance_end="# END Managed by Nasberry appliance mode"
    local disable_comment="# Nasberry appliance mode: disable share"
    local shares_begin="# BEGIN NasberryPi managed shares"
    local shares_end="# END NasberryPi managed shares"
    if ! grep -Fq "$marker_prefix" "$SMB_CONF" && ! grep -Fq "$appliance_begin" "$SMB_CONF" && ! grep -Fq "$appliance_end" "$SMB_CONF" && ! grep -Fq "$appliance_header" "$SMB_CONF" && ! grep -Fq "$appliance_marker" "$SMB_CONF" && ! grep -Fq "$disable_comment" "$SMB_CONF" && ! grep -Fq "$shares_begin" "$SMB_CONF" && ! grep -Fq "$shares_end" "$SMB_CONF"; then
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
    if ! python3 - "$SMB_CONF" "$temp" "$marker_prefix" "$appliance_header" "$appliance_marker" "$appliance_begin" "$appliance_end" "$disable_comment" "$shares_begin" "$shares_end" "$MOUNT_POINT" <<'PY'
import os
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
marker_prefix = sys.argv[3]
appliance_header = sys.argv[4]
appliance_marker = sys.argv[5]
appliance_begin = sys.argv[6]
appliance_end = sys.argv[7]
disable_comment = sys.argv[8]
shares_begin = sys.argv[9]
shares_end = sys.argv[10]
mount_point = sys.argv[11]
user_share_limit = "usershare max shares = 0"
default_public_path = os.path.join(mount_point, "Public")
lines = source.read_text().splitlines(keepends=True)


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def section_name(line):
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return None


def marker_bounds(begin_marker, end_marker, label):
    begins = [index for index, line in enumerate(lines) if line.strip() == begin_marker]
    ends = [index for index, line in enumerate(lines) if line.strip() == end_marker]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1:
        fail(f"{label} markers are malformed")
    begin, end = begins[0], ends[0]
    if begin >= end:
        fail(f"{label} markers are out of order")
    return begin, end


def add_range(ranges, start, end, label):
    if start >= end:
        fail(f"{label} ownership range is empty")
    for existing_start, existing_end, existing_label in ranges:
        if start < existing_end and existing_start < end:
            fail(f"{label} overlaps {existing_label}")
    ranges.append((start, end, label))


def section_end(start):
    end = start + 1
    while end < len(lines) and section_name(lines[end]) is None:
        end += 1
    return end


def legacy_marker_name(line):
    stripped = line.strip()
    if stripped.startswith(marker_prefix):
        return stripped[len(marker_prefix):].strip()
    return None


def marked_share_range(marker_index, share_name):
    section_index = marker_index + 1
    while section_index < len(lines) and not lines[section_index].strip():
        section_index += 1
    found = section_name(lines[section_index]) if section_index < len(lines) else None
    if not found or found.lower() != share_name.lower():
        detail = share_name or "<empty>"
        fail(f"legacy Nasberry share marker is ambiguous: {detail}")
    return marker_index, section_end(section_index)


def contains_index(ranges, index):
    return any(start <= index < end for start, end, _label in ranges)


def samba_sections_outside(ranges):
    sections = []
    index = 0
    while index < len(lines):
        name = section_name(lines[index])
        if name is None:
            index += 1
            continue
        end = section_end(index)
        if not contains_index(ranges, index):
            options = {}
            for raw_line in lines[index + 1:end]:
                if "=" in raw_line:
                    key, value = raw_line.split("=", 1)
                    options[key.strip().lower()] = value.strip()
            sections.append({"name": name, "start": index, "end": end, "options": options})
        index = end
    return sections


def section_is_default_public(section):
    if section["name"].lower() != "public":
        return False
    path = section["options"].get("path", "")
    if os.path.abspath(path) != os.path.abspath(default_public_path):
        return False
    read_only = section["options"].get("read only", "no").lower() in {"yes", "true"}
    return not read_only


ranges = []
current_bounds = marker_bounds(shares_begin, shares_end, "NasberryPi managed Samba section")
if current_bounds is not None:
    add_range(ranges, current_bounds[0], current_bounds[1] + 1, "current NasberryPi managed section")

legacy_bounds = marker_bounds(appliance_begin, appliance_end, "legacy Nasberry appliance section")
if legacy_bounds is not None:
    add_range(ranges, legacy_bounds[0], legacy_bounds[1] + 1, "legacy Nasberry appliance section")

for index, line in enumerate(lines):
    if contains_index(ranges, index):
        continue
    share_name = legacy_marker_name(line)
    if share_name is not None:
        start, end = marked_share_range(index, share_name)
        add_range(ranges, start, end, f"legacy Nasberry share {share_name}")

for index, line in enumerate(lines):
    if contains_index(ranges, index):
        continue
    stripped = line.strip()
    if stripped == disable_comment:
        if index + 1 >= len(lines) or lines[index + 1].strip().lower() != "available = no":
            fail("legacy Nasberry appliance disable marker is ambiguous")
        add_range(ranges, index, index + 2, "legacy Nasberry disable directive")
    elif stripped == appliance_marker:
        fail("standalone legacy Nasberry appliance marker is ambiguous")

legacy_appliance_file = any(line.strip() == appliance_header for line in lines)
if legacy_appliance_file:
    public_sections = [
        section for section in samba_sections_outside(ranges)
        if section["name"].lower() == "public"
    ]
    default_public_sections = [
        section for section in public_sections
        if section_is_default_public(section)
    ]
    if len(public_sections) > 1 or (public_sections and len(default_public_sections) != 1):
        fail("legacy Nasberry appliance Public share is ambiguous")
    for index, line in enumerate(lines):
        if contains_index(ranges, index):
            continue
        stripped = line.strip()
        if stripped == appliance_header:
            add_range(ranges, index, index + 1, "legacy Nasberry appliance header")
        elif stripped.lower() == user_share_limit:
            add_range(ranges, index, index + 1, "legacy Nasberry usershare limit")
    if default_public_sections:
        section = default_public_sections[0]
        add_range(ranges, section["start"], section["end"], "legacy Nasberry appliance Public share")

output = [
    line for index, line in enumerate(lines)
    if not contains_index(ranges, index)
]
destination.write_text("".join(output))
PY
    then
        rm -f "$temp"
        die "Nasberry-managed Samba cleanup could not be performed safely. Live configuration was not changed. Backup: $backup"
    fi
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

remove_command_link "$BIN_PATH"
remove_command_link "$SYSTEM_BIN_PATH"
remove_application_files

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
