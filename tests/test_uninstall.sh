#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/etc/nasberry" "$TMP/etc/samba" "$TMP/mnt/nasberry"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
ln -s "$TMP/opt/nasberry/nasberrypi.py" "$TMP/usr/local/bin/nasberry"
printf 'config\n' > "$TMP/etc/nasberry/config.ini"
cat > "$TMP/etc/samba/smb.conf" <<'EOF'
[global]
   workgroup = WORKGROUP

   # Managed by Nasberry appliance mode
   usershare max shares = 0

# BEGIN Managed by Nasberry appliance mode
[homes]
   # Nasberry appliance mode: disable share
   available = no
[Public]
   path = /mnt/nasberry/Public
# END Managed by Nasberry appliance mode

[OtherShare]
   path = /srv/other
EOF

run_uninstall() {
    env \
        NASBERRY_INSTALL_DIR="$TMP/opt/nasberry" \
        NASBERRY_BIN_PATH="$TMP/usr/local/bin/nasberry" \
        NASBERRY_CONFIG_DIR="$TMP/etc/nasberry" \
        NASBERRY_SMB_CONF="$TMP/etc/samba/smb.conf" \
        NASBERRY_MOUNT_POINT="$TMP/mnt/nasberry" \
        "$ROOT_DIR/uninstall.sh" "$@"
}

run_uninstall --yes
[ ! -e "$TMP/opt/nasberry" ]
[ ! -e "$TMP/usr/local/bin/nasberry" ]
[ -f "$TMP/etc/nasberry/config.ini" ]
grep -Fq '[Public]' "$TMP/etc/samba/smb.conf"

mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/etc/nasberry"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
ln -s "$TMP/opt/nasberry/nasberrypi.py" "$TMP/usr/local/bin/nasberry"
printf 'config\n' > "$TMP/etc/nasberry/config.ini"
run_uninstall --yes --purge --remove-mount-point
[ ! -e "$TMP/etc/nasberry" ]
[ ! -e "$TMP/mnt/nasberry" ]
! grep -Fq '[Public]' "$TMP/etc/samba/smb.conf"
grep -Fq '[OtherShare]' "$TMP/etc/samba/smb.conf"
! grep -Fq 'usershare max shares = 0' "$TMP/etc/samba/smb.conf"
! grep -Fq 'Nasberry appliance mode: disable share' "$TMP/etc/samba/smb.conf"
! grep -Fq 'available = no' "$TMP/etc/samba/smb.conf"

mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/etc/nasberry" "$TMP/mnt/nasberry"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'keep me\n' > "$TMP/mnt/nasberry/user-file.txt"
run_uninstall --dry-run --purge --remove-mount-point --yes >/dev/null
[ -e "$TMP/opt/nasberry/nasberrypi.py" ]
[ -e "$TMP/etc/nasberry" ]
run_uninstall --yes --remove-mount-point >/dev/null
[ -e "$TMP/mnt/nasberry/user-file.txt" ]

echo 'uninstall integration tests passed'
