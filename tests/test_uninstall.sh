#!/bin/bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/bin" "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/usr/bin" "$TMP/etc/nasberry" "$TMP/etc/samba" "$TMP/mnt/nasberry"
cat > "$TMP/bin/testparm" <<'EOF'
#!/bin/sh
[ "${TESTPARM_FAIL:-0}" != 1 ]
EOF
chmod +x "$TMP/bin/testparm"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
ln -s "$TMP/opt/nasberry/nasberrypi.py" "$TMP/usr/local/bin/nasberry"
ln -s "$TMP/opt/nasberry/nasberrypi.py" "$TMP/usr/bin/nasberry"
printf 'config\n' > "$TMP/etc/nasberry/config.ini"
cat > "$TMP/etc/samba/smb.conf" <<'EOF'
[global]
   workgroup = WORKGROUP

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
        PATH="$TMP/bin:$PATH" \
        NASBERRY_INSTALL_DIR="$TMP/opt/nasberry" \
        NASBERRY_BIN_PATH="$TMP/usr/local/bin/nasberry" \
        NASBERRY_SYSTEM_BIN_PATH="$TMP/usr/bin/nasberry" \
        NASBERRY_CONFIG_DIR="$TMP/etc/nasberry" \
        NASBERRY_SMB_CONF="$TMP/etc/samba/smb.conf" \
        NASBERRY_MOUNT_POINT="$TMP/mnt/nasberry" \
        bash "$ROOT_DIR/uninstall.sh" "$@"
}

reset_installed_app() {
    rm -rf "$TMP/opt/nasberry" "$TMP/etc/nasberry"
    rm -rf "$TMP/usr/local/bin/nasberry" "$TMP/usr/bin/nasberry"
    mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/usr/bin" "$TMP/etc/nasberry" "$TMP/mnt/nasberry"
    printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
    printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
    printf 'config\n' > "$TMP/etc/nasberry/config.ini"
    ln -s "$TMP/opt/nasberry/nasberrypi.py" "$TMP/usr/local/bin/nasberry"
}

assert_purge_refuses_smb_cleanup() {
    local name="$1"
    cat > "$TMP/etc/samba/smb.conf"
    cp "$TMP/etc/samba/smb.conf" "$TMP/etc/samba/smb.conf.before-$name"
    if run_uninstall --yes --purge >"$TMP/$name.out" 2>&1; then
        echo "unsafe Samba cleanup was accepted: $name" >&2
        exit 1
    fi
    cmp -s "$TMP/etc/samba/smb.conf.before-$name" "$TMP/etc/samba/smb.conf"
    grep -Fq "Samba cleanup could not be performed safely" "$TMP/$name.out"
}

run_uninstall --yes
[ ! -e "$TMP/opt/nasberry" ]
[ ! -e "$TMP/usr/local/bin/nasberry" ]
[ ! -e "$TMP/usr/bin/nasberry" ]
[ -f "$TMP/etc/nasberry/config.ini" ]
grep -Fq '[Public]' "$TMP/etc/samba/smb.conf"

mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/usr/bin"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
printf 'do not remove\n' > "$TMP/usr/local/bin/nasberry"
ln -s "$TMP/elsewhere/nasberry" "$TMP/usr/bin/nasberry"
run_uninstall --yes >/dev/null
grep -Fq 'do not remove' "$TMP/usr/local/bin/nasberry"
[ -L "$TMP/usr/bin/nasberry" ]
[ "$(readlink "$TMP/usr/bin/nasberry")" = "$TMP/elsewhere/nasberry" ]
[ ! -e "$TMP/opt/nasberry" ]
rm -f "$TMP/usr/local/bin/nasberry" "$TMP/usr/bin/nasberry"

mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/usr/bin"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
ln -s "$TMP/other-target" "$TMP/usr/local/bin/nasberry"
ln -s "$TMP/opt/nasberry/nasberrypi.py" "$TMP/usr/bin/nasberry"
run_uninstall --yes >/dev/null
[ -L "$TMP/usr/local/bin/nasberry" ]
[ "$(readlink "$TMP/usr/local/bin/nasberry")" = "$TMP/other-target" ]
[ ! -e "$TMP/usr/bin/nasberry" ]
[ ! -e "$TMP/opt/nasberry" ]
rm -f "$TMP/usr/local/bin/nasberry"

mkdir -p "$TMP/opt/nasberry/unexpected-dir" "$TMP/usr/local/bin"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
printf 'keep me\n' > "$TMP/opt/nasberry/my-notes.txt"
printf 'nested\n' > "$TMP/opt/nasberry/unexpected-dir/sentinel.txt"
ln -s "$TMP/opt/nasberry/nasberrypi.py" "$TMP/usr/local/bin/nasberry"
run_uninstall --yes >/dev/null
[ -d "$TMP/opt/nasberry" ]
[ -f "$TMP/opt/nasberry/my-notes.txt" ]
[ -f "$TMP/opt/nasberry/unexpected-dir/sentinel.txt" ]
[ ! -e "$TMP/opt/nasberry/nasberrypi.py" ]
[ ! -e "$TMP/opt/nasberry/uninstall.sh" ]
[ ! -e "$TMP/usr/local/bin/nasberry" ]
rm -rf "$TMP/opt/nasberry" "$TMP/usr/local/bin/nasberry"
mkdir -p "$TMP/opt/nasberry"

rm -rf "$TMP/opt/nasberry"
mkdir -p "$TMP/real-install-target" "$TMP/usr/local/bin"
printf 'outside app\n' > "$TMP/real-install-target/nasberrypi.py"
printf 'outside uninstall\n' > "$TMP/real-install-target/uninstall.sh"
ln -s "$TMP/real-install-target" "$TMP/opt/nasberry"
run_uninstall --yes >/dev/null
[ -L "$TMP/opt/nasberry" ]
grep -Fq 'outside app' "$TMP/real-install-target/nasberrypi.py"
grep -Fq 'outside uninstall' "$TMP/real-install-target/uninstall.sh"
rm "$TMP/opt/nasberry"
mkdir -p "$TMP/opt/nasberry"

rm -rf "$TMP/opt/nasberry"
printf 'not a directory\n' > "$TMP/opt/nasberry"
run_uninstall --yes >/dev/null
grep -Fq 'not a directory' "$TMP/opt/nasberry"
rm -f "$TMP/opt/nasberry"
mkdir -p "$TMP/opt/nasberry"

mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/etc/nasberry"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
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

# Purge the exact Public-only appliance configuration generated by current Nasberry.
mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/etc/nasberry" "$TMP/mnt/nasberry"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
printf 'config\n' > "$TMP/etc/nasberry/config.ini"
cat > "$TMP/etc/samba/smb.conf" <<EOF
# Managed by Nasberry appliance mode. Previous config is saved before replacement.
[global]
   workgroup = WORKGROUP
   usershare max shares = 0

[Public]
   path = $TMP/mnt/nasberry/Public
EOF
cp "$TMP/etc/samba/smb.conf" "$TMP/etc/samba/smb.conf.before-failed-purge"
if TESTPARM_FAIL=1 run_uninstall --yes --purge >/dev/null 2>&1; then
    echo 'invalid Samba purge candidate was accepted' >&2
    exit 1
fi
cmp -s "$TMP/etc/samba/smb.conf.before-failed-purge" "$TMP/etc/samba/smb.conf"
[ -e "$TMP/opt/nasberry/nasberrypi.py" ]
run_uninstall --yes --purge
! grep -Fq '[Public]' "$TMP/etc/samba/smb.conf"
! grep -Fq 'usershare max shares = 0' "$TMP/etc/samba/smb.conf"
grep -Fq '[global]' "$TMP/etc/samba/smb.conf"

mkdir -p "$TMP/opt/nasberry" "$TMP/usr/local/bin" "$TMP/etc/nasberry" "$TMP/mnt/nasberry"
printf 'app\n' > "$TMP/opt/nasberry/nasberrypi.py"
printf 'uninstaller\n' > "$TMP/opt/nasberry/uninstall.sh"
printf 'config\n' > "$TMP/etc/nasberry/config.ini"
cat > "$TMP/etc/samba/smb.conf" <<'EOF'
[global]
   workgroup = WORKGROUP

[BeforeShare]
   path = /srv/before

# BEGIN NasberryPi managed shares
[Public]
   path = /mnt/nasberry/Public
[Media]
   path = /mnt/nasberry/Media
# END NasberryPi managed shares

[OtherShare]
   path = /srv/other
EOF
run_uninstall --yes --purge
! grep -Fq '[Media]' "$TMP/etc/samba/smb.conf"
! grep -Fq 'BEGIN NasberryPi managed shares' "$TMP/etc/samba/smb.conf"
grep -Fq '[BeforeShare]' "$TMP/etc/samba/smb.conf"
grep -Fq '[OtherShare]' "$TMP/etc/samba/smb.conf"

reset_installed_app
assert_purge_refuses_smb_cleanup current-begin-without-end <<'EOF'
[global]
   workgroup = WORKGROUP

# BEGIN NasberryPi managed shares
[Public]
   path = /mnt/nasberry/Public

[UnrelatedShare]
   path = /srv/unrelated
EOF
grep -Fq '[UnrelatedShare]' "$TMP/etc/samba/smb.conf"

reset_installed_app
assert_purge_refuses_smb_cleanup current-end-without-begin <<'EOF'
[global]
   workgroup = WORKGROUP

# END NasberryPi managed shares

[UnrelatedShare]
   path = /srv/unrelated
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup duplicate-current-begin <<'EOF'
[global]
   workgroup = WORKGROUP

# BEGIN NasberryPi managed shares
# BEGIN NasberryPi managed shares
[Public]
   path = /mnt/nasberry/Public
# END NasberryPi managed shares
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup duplicate-current-end <<'EOF'
[global]
   workgroup = WORKGROUP

# BEGIN NasberryPi managed shares
[Public]
   path = /mnt/nasberry/Public
# END NasberryPi managed shares
# END NasberryPi managed shares
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup current-markers-out-of-order <<'EOF'
[global]
   workgroup = WORKGROUP

# END NasberryPi managed shares
[Public]
   path = /mnt/nasberry/Public
# BEGIN NasberryPi managed shares
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup legacy-begin-without-end <<'EOF'
[global]
   workgroup = WORKGROUP

# BEGIN Managed by Nasberry appliance mode
[Public]
   path = /mnt/nasberry/Public

[UnrelatedShare]
   path = /srv/unrelated
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup legacy-end-without-begin <<'EOF'
[global]
   workgroup = WORKGROUP

# END Managed by Nasberry appliance mode

[UnrelatedShare]
   path = /srv/unrelated
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup duplicate-legacy-begin <<'EOF'
[global]
   workgroup = WORKGROUP

# BEGIN Managed by Nasberry appliance mode
# BEGIN Managed by Nasberry appliance mode
[Public]
   path = /mnt/nasberry/Public
# END Managed by Nasberry appliance mode
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup duplicate-legacy-end <<'EOF'
[global]
   workgroup = WORKGROUP

# BEGIN Managed by Nasberry appliance mode
[Public]
   path = /mnt/nasberry/Public
# END Managed by Nasberry appliance mode
# END Managed by Nasberry appliance mode
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup legacy-markers-out-of-order <<'EOF'
[global]
   workgroup = WORKGROUP

# END Managed by Nasberry appliance mode
[Public]
   path = /mnt/nasberry/Public
# BEGIN Managed by Nasberry appliance mode
EOF

reset_installed_app
cat > "$TMP/etc/samba/smb.conf" <<'EOF'
[global]
   workgroup = WORKGROUP
   usershare max shares = 0

# BEGIN NasberryPi managed shares
[Public]
   path = /mnt/nasberry/Public
# END NasberryPi managed shares

[OtherShare]
   path = /srv/other
EOF
run_uninstall --yes --purge
grep -Fq 'usershare max shares = 0' "$TMP/etc/samba/smb.conf"
grep -Fq '[OtherShare]' "$TMP/etc/samba/smb.conf"
! grep -Fq 'BEGIN NasberryPi managed shares' "$TMP/etc/samba/smb.conf"

reset_installed_app
assert_purge_refuses_smb_cleanup ambiguous-legacy-public <<'EOF'
# Managed by Nasberry appliance mode. Previous config is saved before replacement.
[global]
   workgroup = WORKGROUP
   usershare max shares = 0

[Public]
   path = /mnt/nasberry/Public

[Public]
   path = /srv/unrelated-public
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup appliance-public-wrong-path <<'EOF'
# Managed by Nasberry appliance mode. Previous config is saved before replacement.
[global]
   workgroup = WORKGROUP
   usershare max shares = 0

[Public]
   path = /srv/unrelated-public

[OtherShare]
   path = /srv/other
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup appliance-public-read-only <<EOF
# Managed by Nasberry appliance mode. Previous config is saved before replacement.
[global]
   workgroup = WORKGROUP
   usershare max shares = 0

[Public]
   path = $TMP/mnt/nasberry/Public
   read only = yes

[OtherShare]
   path = /srv/other
EOF

reset_installed_app
assert_purge_refuses_smb_cleanup malformed-disable-comment <<'EOF'
[global]
   workgroup = WORKGROUP

# Nasberry appliance mode: disable share
   some unrelated setting = value

[OtherShare]
   path = /srv/other
EOF

reset_installed_app
printf 'keep me\n' > "$TMP/mnt/nasberry/user-file.txt"
run_uninstall --dry-run --purge --remove-mount-point --yes >/dev/null
[ -e "$TMP/opt/nasberry/nasberrypi.py" ]
[ -e "$TMP/opt/nasberry/uninstall.sh" ]
[ -L "$TMP/usr/local/bin/nasberry" ]
[ -e "$TMP/etc/nasberry" ]
run_uninstall --yes --remove-mount-point >/dev/null
[ -e "$TMP/mnt/nasberry/user-file.txt" ]

if env PATH="$TMP/bin:$PATH" NASBERRY_INSTALL_DIR=/ NASBERRY_BIN_PATH="$TMP/usr/local/bin/nasberry" NASBERRY_CONFIG_DIR="$TMP/etc/nasberry" NASBERRY_MOUNT_POINT="$TMP/mnt/nasberry" bash "$ROOT_DIR/uninstall.sh" --yes >/dev/null 2>&1; then
    echo 'unsafe removal path was not rejected' >&2
    exit 1
fi

echo 'uninstall integration tests passed'
