#!/usr/bin/env python3

# =========================================================
# nasberrypi
# NASBERRY NETWORK STORAGE SYSTEM 
#
# Copyright (c) 2026 WastelandSYS
# Licensed under GPLv3
# =========================================================

import argparse
import configparser
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

APP_VERSION = "0.2.5"
DEFAULT_CONFIG_FILE = "/etc/nasberry/config.ini" if os.geteuid() == 0 else "~/.config/nasberry/config.ini"
CONFIG_FILE = Path(os.path.expanduser(os.environ.get("NASBERRY_CONFIG_FILE", DEFAULT_CONFIG_FILE)))
DEFAULTS = {
    "device": "/dev/disk/by-label/NasberryDRV",
    "mount_point": "/mnt/nasberry",
    "share_name": "Public",
    "share_user": "",
    "samba_service": "smbd",
    "samba_services": "smbd,nmbd,winbind",
    "state_file": "~/.nasberry_state.log",
    "check_delay": "2",
    "safe_mode_on_start": "false",
    "pin_hash": "",
}

state = {"running": True}
config = configparser.ConfigParser()
config["nasberry"] = DEFAULTS.copy()
if CONFIG_FILE.exists():
    config.read(CONFIG_FILE)
settings = config["nasberry"]


def setting(name, env_name=None):
    return os.environ.get(env_name or f"NASBERRY_{name.upper()}", settings.get(name, DEFAULTS[name]))


def refresh_settings():
    global DEVICE, MOUNT_POINT, SHARE_NAME, SHARE_USER, SAMBA_SERVICE, SAMBA_SERVICES, STATE_FILE, CHECK_DELAY, SAFE_MODE_ON_START
    DEVICE = setting("device")
    MOUNT_POINT = setting("mount_point")
    SHARE_NAME = "Public"
    SHARE_USER = setting("share_user")
    SAMBA_SERVICE = setting("samba_service")
    SAMBA_SERVICES = [item.strip() for item in setting("samba_services").split(",") if item.strip()]
    STATE_FILE = os.path.expanduser(setting("state_file"))
    try:
        CHECK_DELAY = max(0, int(setting("check_delay")))
    except ValueError:
        CHECK_DELAY = 2
    SAFE_MODE_ON_START = setting("safe_mode_on_start").lower() in {"1", "true", "yes", "on"}


refresh_settings()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"Command failed: {exc}")
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def sudo_cmd(*cmd):
    return list(cmd) if os.geteuid() == 0 else ["sudo", *cmd]


def command_exists(command):
    return shutil.which(command) is not None


def clear():
    if sys.stdout.isatty() and os.environ.get("TERM"):
        os.system("clear")


def pause():
    input("\n  Press Enter to continue...")


def save_config():
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    with temp.open("w") as handle:
        config.write(handle)
    os.chmod(temp, 0o600)
    temp.replace(CONFIG_FILE)


def lsblk_devices():
    if not command_exists("lsblk"):
        return []
    result = run(["lsblk", "--json", "--paths", "--fs", "--output", "NAME,PATH,LABEL,UUID,MOUNTPOINTS,RM,TYPE,FSTYPE,SIZE"])
    if result.returncode != 0:
        return []
    try:
        tree = json.loads(result.stdout).get("blockdevices", [])
    except json.JSONDecodeError:
        return []

    devices = []
    def walk(items, parent_removable=False):
        for item in items:
            removable = bool(item.get("rm")) or parent_removable
            item["removable"] = removable
            path = item.get("path") or item.get("name") or ""
            filesystem = (item.get("fstype") or "").lower()
            if (
                item.get("type") in {"part", "disk"}
                and filesystem not in {"", "swap"}
                and not path.startswith("/dev/zram")
                and not any(
                    point in {"/", "/boot", "/boot/firmware", "/home"}
                    for point in (item.get("mountpoints") or [])
                    if point
                )
            ):
                devices.append(item)
            walk(item.get("children") or [], removable)
    walk(tree)
    return devices


def candidate_score(device):
    label = (device.get("label") or "").lower()
    score = 100 if device.get("removable") else 0
    if any(word in label for word in ("nasberry", "nas", "storage", "share")):
        score += 50
    if device.get("uuid"):
        score += 10
    return score


def detect_storage_devices():
    return sorted(lsblk_devices(), key=candidate_score, reverse=True)


def device_mount_points(device_path=None):
    target = os.path.realpath(device_path or DEVICE)
    for device in lsblk_devices():
        if os.path.realpath(device.get("path") or device.get("name") or "") == target:
            return [point for point in (device.get("mountpoints") or []) if point]
    return []


def storage_filesystem():
    target = os.path.realpath(DEVICE)
    device = next((item for item in lsblk_devices() if os.path.realpath(item.get("path") or "") == target), None)
    return (device.get("fstype") or "").lower() if device else ""


def filesystem_uses_mount_permissions():
    return storage_filesystem() in {"exfat", "fat", "msdos", "ntfs", "ntfs3", "fuseblk", "vfat"}


def storage_mount_options():
    if not filesystem_uses_mount_permissions() or not SHARE_USER:
        return []
    try:
        owner = pwd.getpwnam(SHARE_USER)
    except KeyError:
        return []
    return ["-o", f"uid={owner.pw_uid},gid={owner.pw_gid},umask=0002"]


def is_mounted():
    return os.path.ismount(MOUNT_POINT)


def ensure_mount_point():
    try:
        Path(MOUNT_POINT).mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        log(f"✖ Could not create mount point {MOUNT_POINT}: {exc}")
        return False


def device_exists():
    return os.path.exists(DEVICE)


def cleanup_other_mounts():
    mounts = [point for point in device_mount_points() if os.path.realpath(point) != os.path.realpath(MOUNT_POINT)]
    for point in mounts:
        log(f"Detected storage mounted at {point}; moving it into NAS mode...")
        result = run(sudo_cmd("umount", point))
        if result.returncode != 0:
            log(f"✖ Could not unmount {point}: {result.stderr.strip() or 'device may be busy'}")
            return False
    return True


def mount_storage(repair_permissions=False):
    if not ensure_mount_point() or not cleanup_other_mounts():
        write_state(False, service_active())
        return False
    options = storage_mount_options()
    if is_mounted() and repair_permissions and options:
        if service_active() and not stop_share():
            log("✖ Could not stop sharing before repairing storage permissions")
            return False
        result = run(sudo_cmd("umount", MOUNT_POINT))
        if result.returncode != 0:
            log(f"✖ Could not remount storage: {result.stderr.strip() or 'device may be busy'}")
            return False
    if is_mounted():
        log("✔ Storage is already mounted")
        write_state(True, service_active())
        return True
    if not device_exists():
        candidates = detect_storage_devices()
        log(f"✖ Configured storage device not found: {DEVICE}")
        log("Run 'nasberry setup' to select a drive." if candidates else "Connect a formatted USB drive, then run 'nasberry setup'.")
        write_state(False, service_active())
        return False
    log(f"Mounting {DEVICE} at {MOUNT_POINT}...")
    result = run(sudo_cmd("mount", *options, DEVICE, MOUNT_POINT))
    time.sleep(CHECK_DELAY)
    mounted = result.returncode == 0 and is_mounted()
    if mounted:
        log(f"✔ Storage mounted at {MOUNT_POINT}")
    else:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown mount error"
        log(f"✖ Mount failed: {detail}")
        log("Run 'nasberry doctor' for suggested fixes.")
    write_state(mounted, service_active())
    return mounted


def service_exists(service):
    if not command_exists("systemctl"):
        return False
    result = run(["systemctl", "list-unit-files", f"{service}.service", "--no-legend"])
    return result.returncode == 0 and f"{service}.service" in result.stdout


def service_active():
    if not command_exists("systemctl"):
        return False
    result = run(["systemctl", "is-active", SAMBA_SERVICE])
    return result.returncode == 0 and result.stdout.strip() == "active"


def enforce_boot_safety():
    log("Enforcing requested NAS safe-mode policy...")
    success = True
    for service in SAMBA_SERVICES:
        if not service_exists(service):
            continue
        for action in ("stop", "disable"):
            result = run(sudo_cmd("systemctl", action, service))
            if result.returncode != 0:
                success = False
                log(f"✖ Could not {action} {service}: {result.stderr.strip()}")
    log("✔ Safe-mode policy enforced" if success else "⚠ Safe-mode policy was only partially applied")
    return success


def unmount_storage():
    if not is_mounted():
        log("✔ Storage is already unmounted")
        write_state(False, service_active())
        return True
    if service_active() and not stop_share():
        log("✖ Refusing to unmount while the share is still active")
        return False
    log("Safely unmounting storage...")
    result = run(sudo_cmd("umount", MOUNT_POINT))
    time.sleep(CHECK_DELAY)
    unmounted = result.returncode == 0 and not is_mounted()
    if unmounted:
        log("✔ Storage safely unmounted")
    else:
        log(f"✖ Unmount failed: {result.stderr.strip() or 'device may be busy'}")
    write_state(is_mounted(), service_active())
    return unmounted


def public_share_path():
    return os.path.join(MOUNT_POINT, "Public")


def storage_folder_path(name):
    return os.path.join(MOUNT_POINT, name)


def ensure_storage_layout():
    if not SHARE_USER:
        log("✖ No Samba user is configured")
        return False
    if not is_mounted():
        log("✖ Refusing to create the storage layout while storage is unmounted")
        return False
    try:
        owner = pwd.getpwnam(SHARE_USER)
        mount_permissions = filesystem_uses_mount_permissions()
        for name, mode in (("Public", 0o775), ("Private", 0o700), ("Backups", 0o700)):
            folder = Path(storage_folder_path(name))
            if folder.is_symlink():
                raise OSError(f"{name} folder must not be a symbolic link")
            folder.mkdir(parents=True, exist_ok=True)
            if not mount_permissions:
                os.chown(folder, owner.pw_uid, owner.pw_gid)
                os.chmod(folder, mode)
            entry = folder.stat()
            if entry.st_uid != owner.pw_uid or not (entry.st_mode & 0o200):
                raise OSError(f"{name} folder is not owned and writable by the configured share user")
        return True
    except (KeyError, OSError) as exc:
        log(f"✖ Could not prepare storage folders under {MOUNT_POINT}: {exc}")
        return False


def ensure_public_folder():
    """Compatibility wrapper used by the online path; setup/repair manage the full layout."""
    if not SHARE_USER:
        log("✖ No Samba user is configured")
        return False
    if not is_mounted():
        log("✖ Refusing to create Public while storage is unmounted")
        return False
    try:
        owner = pwd.getpwnam(SHARE_USER)
        folder = Path(public_share_path())
        if folder.is_symlink():
            raise OSError("Public folder must not be a symbolic link")
        folder.mkdir(parents=True, exist_ok=True)
        if not filesystem_uses_mount_permissions():
            os.chown(folder, owner.pw_uid, owner.pw_gid)
            os.chmod(folder, 0o775)
        entry = folder.stat()
        if entry.st_uid != owner.pw_uid or not (entry.st_mode & 0o200):
            raise OSError("Public folder is not owned and writable by the configured share user")
        return True
    except (KeyError, OSError) as exc:
        log(f"✖ Could not make {public_share_path()} writable by {SHARE_USER}: {exc}")
        return False


def samba_shares():
    if not command_exists("testparm"):
        return None
    result = run(["testparm", "-s"])
    if result.returncode != 0:
        return None
    shares = {}
    current = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if current.lower() != "global":
                shares[current] = {}
        elif current in shares and "=" in line:
            key, value = line.split("=", 1)
            shares[current][key.strip().lower()] = value.strip()
    return shares


def samba_config_valid():
    shares = samba_shares()
    if shares is None:
        return False, "testparm could not read the Samba configuration"
    public = shares.get("Public")
    if not public:
        return False, "share [Public] was not found"
    configured_path = public.get("path", "")
    expected_path = public_share_path()
    if os.path.abspath(configured_path) != os.path.abspath(expected_path):
        return False, f"share [Public] points to {configured_path or 'no path'}, not {expected_path}"
    active = [name for name, options in shares.items() if options.get("available", "yes").lower() not in {"no", "false"}]
    if set(active) != {"Public"}:
        detail = ", ".join(f"[{name}]" for name in active) or "none"
        return False, f"active Samba shares are {detail}; only [Public] is allowed"
    return True, configured_path


def start_share():
    if not service_exists(SAMBA_SERVICE):
        log(f"✖ Samba service '{SAMBA_SERVICE}' was not found. Run 'nasberry doctor'.")
        write_state(is_mounted(), False)
        return False
    if not is_mounted() and not mount_storage():
        log("✖ Refusing to start sharing without mounted storage")
        return False
    if not ensure_public_folder():
        log("✖ Refusing to start sharing without a safe, writable Public folder")
        return False
    valid, reason = samba_config_valid()
    if not valid:
        log(f"✖ Samba configuration is not ready: {reason}")
        log("Run 'sudo nasberry repair-samba' to recreate it.")
        return False
    log("Starting file sharing...")
    result = run(sudo_cmd("systemctl", "start", SAMBA_SERVICE))
    time.sleep(1)
    active = result.returncode == 0 and service_active()
    if active:
        log("✔ NAS SHARE ONLINE")
        print_connection_info()
    else:
        log(f"✖ Failed to start sharing: {result.stderr.strip() or 'check systemctl status'}")
    write_state(is_mounted(), active)
    return active


def stop_share():
    if not service_exists(SAMBA_SERVICE):
        write_state(is_mounted(), False)
        return True
    log("Stopping file sharing...")
    result = run(sudo_cmd("systemctl", "stop", SAMBA_SERVICE))
    time.sleep(1)
    stopped = result.returncode == 0 and not service_active()
    log("✔ NAS SHARE OFFLINE" if stopped else f"✖ Failed to stop sharing: {result.stderr.strip()}")
    write_state(is_mounted(), service_active())
    return stopped


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_pin_value(pin, stored):
    try:
        algorithm, salt, expected = stored.split("$", 2)
        return algorithm == "pbkdf2_sha256" and hmac.compare_digest(hash_pin(pin, salt).split("$", 2)[2], expected)
    except ValueError:
        return False


def verify_pin():
    stored = settings.get("pin_hash", "")
    if not stored:
        log("✖ No security PIN has been configured. Run 'nasberry setup'.")
        return False
    return verify_pin_value(getpass.getpass("Enter NAS PIN: ").strip(), stored)


def panic_lock():
    log("!!! EMERGENCY LOCK ACTIVATED !!!")
    return stop_share() and unmount_storage()


def write_state(mounted, shared):
    try:
        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE_FILE).write_text(
            f"MOUNTED={int(mounted)}\nSHARED={int(shared)}\nDEVICE={DEVICE}\nMOUNT_POINT={MOUNT_POINT}\nUPDATED_AT={datetime.now().isoformat(timespec='seconds')}\n"
        )
    except OSError as exc:
        log(f"⚠ Could not write state file: {exc}")


def disk_usage():
    if not is_mounted():
        return "Not mounted"
    try:
        usage = shutil.disk_usage(MOUNT_POINT)
        return f"{usage.free / (1024 ** 3):.1f}G free / {usage.total / (1024 ** 3):.1f}G"
    except OSError:
        return "Unavailable"


def usable_address(address):
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.version == 4 and not (parsed.is_loopback or parsed.is_link_local)


def local_addresses():
    addresses = []

    if command_exists("ip"):
        result = run(["ip", "-json", "-4", "address", "show", "scope", "global"])
        if result.returncode == 0:
            try:
                interfaces = json.loads(result.stdout)
                for interface in interfaces:
                    for info in interface.get("addr_info", []):
                        address = info.get("local")
                        if usable_address(address) and address not in addresses:
                            addresses.append(address)
            except json.JSONDecodeError:
                pass

    if not addresses and command_exists("hostname"):
        result = run(["hostname", "-I"])
        if result.returncode == 0:
            addresses.extend(address for address in result.stdout.split() if usable_address(address))

    if not addresses:
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = item[4][0]
                if usable_address(address) and address not in addresses:
                    addresses.append(address)
        except socket.gaierror:
            pass
    return addresses


def print_connection_info():
    addresses = local_addresses()
    if not addresses:
        log("Network address unavailable; run 'nasberry doctor'.")
        return
    print("\nConnect from another device:")
    for address in addresses:
        print(f"  Windows: \\\\{address}\\{SHARE_NAME}")
        print(f"  macOS/Linux: smb://{address}/{SHARE_NAME}")


def check(label, ok, detail, fix=""):
    symbol = "✔" if ok else "✖"
    print(f"{symbol} {label}: {detail}")
    if not ok and fix:
        print(f"    Fix: {fix}")
    return ok


def public_folder_access_valid():
    if not is_mounted():
        return True, "not checked while storage is safely unmounted"
    folder = Path(public_share_path())
    if folder.is_symlink():
        return False, "Public folder is a symbolic link"
    if not folder.is_dir():
        return False, f"missing: {folder}"
    if not SHARE_USER:
        return False, "no configured share user"
    try:
        owner = pwd.getpwnam(SHARE_USER)
        entry = folder.stat()
    except (KeyError, OSError) as exc:
        return False, str(exc)
    if entry.st_uid != owner.pw_uid:
        return False, f"owned by UID {entry.st_uid}, expected {owner.pw_uid} ({SHARE_USER})"
    if not (entry.st_mode & 0o200):
        return False, f"not writable by owner {SHARE_USER}"
    return True, f"writable by {SHARE_USER}"


def protected_folder_status(name):
    if not is_mounted():
        return True, "not checked while storage is safely unmounted"
    folder = Path(storage_folder_path(name))
    if folder.is_symlink():
        return False, f"{name} folder is a symbolic link"
    if not folder.is_dir():
        return False, f"missing: {folder}"
    if filesystem_uses_mount_permissions():
        filesystem = storage_filesystem() or "this filesystem"
        return True, f"local-only; {filesystem} has no per-folder Unix permissions"
    if not SHARE_USER:
        return False, "no configured share user"
    try:
        owner = pwd.getpwnam(SHARE_USER)
        entry = folder.stat()
    except (KeyError, OSError) as exc:
        return False, str(exc)
    mode = entry.st_mode & 0o777
    if entry.st_uid != owner.pw_uid or mode != 0o700:
        return False, f"expected owner {SHARE_USER} and mode 0700; found UID {entry.st_uid} mode {mode:04o}"
    return True, f"local-only, owned by {SHARE_USER}, mode 0700"


def samba_account_valid():
    if not SHARE_USER:
        return False, "no configured share user"
    if not command_exists("pdbedit"):
        return False, "pdbedit is unavailable"
    result = run(["pdbedit", "-L", "-v", SHARE_USER])
    if result.returncode != 0:
        return False, f"Samba account {SHARE_USER!r} was not found"
    flags = next((line.split(":", 1)[1].strip() for line in result.stdout.splitlines() if line.strip().lower().startswith("account flags:")), "")
    if "D" in flags:
        return False, f"Samba account {SHARE_USER!r} is disabled"
    return True, f"enabled for {SHARE_USER}"


def print_windows_credential_hint():
    print("\nIf Windows previously connected with a different Samba password:")
    print("  1. Open Command Prompt on Windows.")
    print("  2. Run: net use * /delete /y")
    print("  3. Reconnect to the Public share using the new Samba password.")


def doctor():
    print("Nasberry diagnostics\n====================")
    results = []
    app_path = str(Path(__file__).resolve())
    results.append(check("Nasberry application", True, f"v{APP_VERSION} at {app_path}"))
    results.append(check("Configuration", CONFIG_FILE.exists(), str(CONFIG_FILE), "Run 'sudo nasberry setup'."))
    results.append(check("Privileges", os.geteuid() == 0 or command_exists("sudo"), "root/sudo available" if os.geteuid() == 0 or command_exists("sudo") else "sudo unavailable", "Run Nasberry as root."))
    for command in ("mount", "umount", "lsblk", "systemctl", "testparm", "ip"):
        results.append(check(f"Command {command}", command_exists(command), shutil.which(command) or "missing", "Re-run install.sh."))
    results.append(check("Storage device", device_exists(), DEVICE, "Connect the drive or run 'sudo nasberry setup'."))
    results.append(check("Mount point", os.path.isdir(MOUNT_POINT), MOUNT_POINT, f"Create it with: sudo mkdir -p {MOUNT_POINT}"))
    results.append(check("Samba service", service_exists(SAMBA_SERVICE), SAMBA_SERVICE, "Install Samba or choose the correct service in setup."))
    valid, reason = samba_config_valid()
    results.append(check("Samba share", valid, reason, "Run 'sudo nasberry repair-samba' to recreate it."))
    public_ok, public_detail = public_folder_access_valid()
    results.append(check("Public folder access", public_ok, public_detail, "Run 'sudo nasberry repair-samba' to repair it."))
    for folder_name in ("Private", "Backups"):
        protected_ok, protected_detail = protected_folder_status(folder_name)
        results.append(check(f"{folder_name} folder protection", protected_ok, protected_detail, "Run 'sudo nasberry repair-samba' to create or repair it."))
    account_ok, account_detail = samba_account_valid()
    results.append(check("Samba account", account_ok, account_detail, f"Run 'sudo smbpasswd -a {SHARE_USER}' to create or reset it." if SHARE_USER else "Run 'sudo nasberry setup'."))
    print_connection_info()
    print(f"\nResult: {sum(results)}/{len(results)} checks passed")
    return all(results)


def choose_device(non_interactive=False):
    candidates = detect_storage_devices()
    if not candidates:
        log("✖ No suitable formatted storage drives were detected.")
        return None
    if non_interactive:
        return candidates[0]
    print("\nDetected storage drives:")
    for index, item in enumerate(candidates, 1):
        print(f"  {index}) {item.get('path')}  label={item.get('label') or '-'}  size={item.get('size') or '-'}  filesystem={item.get('fstype')}")
    answer = input(f"Select drive [1-{len(candidates)}] (default 1): ").strip() or "1"
    try:
        return candidates[int(answer) - 1]
    except (ValueError, IndexError):
        log("✖ Invalid drive selection")
        return None


def restart_samba_service():
    if not service_exists(SAMBA_SERVICE):
        return True
    result = run(sudo_cmd("systemctl", "restart", SAMBA_SERVICE))
    if result.returncode != 0:
        log(f"✖ Samba restart failed: {result.stderr.strip()}")
        return False
    return True


def repair_samba_share():
    if os.geteuid() != 0:
        log("✖ Samba repair must run as root: sudo nasberry repair-samba")
        return False
    if not SHARE_USER:
        log("✖ No Samba user is configured. Run 'sudo nasberry setup' first.")
        return False
    if not mount_storage(repair_permissions=True) or not ensure_storage_layout() or not configure_samba_share():
        log("✖ Samba repair failed. Review the validation error above.")
        return False
    if not restart_samba_service():
        return False
    log("✔ Samba share repaired and validated")
    return True


def appliance_samba_config():
    return f"""# Managed by Nasberry appliance mode. Previous config is saved before replacement.
[global]
   workgroup = WORKGROUP
   server role = standalone server
   security = user
   map to guest = never
   usershare max shares = 0
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes

[Public]
   path = {public_share_path()}
   browseable = yes
   available = yes
   read only = no
   guest ok = no
   valid users = {SHARE_USER}
   force user = {SHARE_USER}
   follow symlinks = no
   wide links = no
   create mask = 0664
   directory mask = 0775
"""


def configure_samba_share():
    smb_file = Path("/etc/samba/smb.conf")
    if not smb_file.exists():
        log("✖ /etc/samba/smb.conf was not found")
        return False
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup = smb_file.with_name(f"{smb_file.name}.nasberry.{timestamp}.bak")
    candidate = smb_file.with_name(f"{smb_file.name}.nasberry.candidate")
    shutil.copy2(smb_file, backup)
    log(f"Preserved previous Samba configuration at {backup}")
    candidate.write_text(appliance_samba_config())
    syntax = run(["testparm", "-s", str(candidate)])
    if syntax.returncode != 0:
        candidate.unlink(missing_ok=True)
        detail = syntax.stderr.strip() or syntax.stdout.strip() or "testparm rejected the candidate configuration"
        log(f"✖ Samba config validation failed: {detail}")
        return False
    os.replace(candidate, smb_file)
    valid, reason = samba_config_valid()
    if valid:
        log("✔ Samba configured with only [Public] active")
        return True
    shutil.copy2(backup, smb_file)
    log(f"✖ Samba config validation failed: {reason}")
    log("✔ Restored the previous Samba configuration")
    return False


def setup(non_interactive=False, skip_pin=False):
    if os.geteuid() != 0:
        log("✖ Setup changes system files and must run as root: sudo nasberry setup")
        return False
    selected = choose_device(non_interactive)
    if not selected:
        return False
    settings["device"] = f"/dev/disk/by-uuid/{selected['uuid']}" if selected.get("uuid") else selected.get("path")
    settings["mount_point"] = MOUNT_POINT
    settings["share_name"] = "Public"
    if not non_interactive:
        default_user = os.environ.get("SUDO_USER") or getpass.getuser()
        share_user = input(f"Linux user allowed to access the share [{default_user}]: ").strip() or default_user
        try:
            pwd.getpwnam(share_user)
        except KeyError:
            log(f"✖ Linux user {share_user!r} does not exist")
            return False
        settings["share_user"] = share_user
    if not skip_pin:
        if non_interactive:
            log("✖ Non-interactive setup requires --skip-pin; run interactive setup afterward to set a PIN.")
            return False
        first = getpass.getpass("Create a new NAS PIN (at least 4 characters): ").strip()
        second = getpass.getpass("Confirm NAS PIN: ").strip()
        if len(first) < 4 or first != second:
            log("✖ PINs did not match or were too short")
            return False
        settings["pin_hash"] = hash_pin(first)
    save_config()
    refresh_settings()
    configured = mount_storage(repair_permissions=True) and ensure_storage_layout() and configure_samba_share()
    password_updated = False
    if configured and settings.get("share_user") and not non_interactive and command_exists("smbpasswd"):
        log(f"Set the Samba network password for {settings['share_user']}:")
        password_result = subprocess.run(["smbpasswd", "-a", settings["share_user"]], check=False)
        configured = password_result.returncode == 0
        password_updated = configured
        if not configured:
            log("✖ Samba password setup failed")
    if configured:
        configured = restart_samba_service()
    if configured:
        log(f"✔ Setup complete; configuration saved to {CONFIG_FILE}")
        if password_updated:
            print_windows_credential_hint()
    else:
        log(f"✖ Setup incomplete. Core settings were saved to {CONFIG_FILE}, but Samba is not ready.")
        log("Update/reinstall Nasberry, then run 'sudo nasberry repair-samba'.")
    return configured


def banner():
    print(f"\nNASBERRY NETWORK STORAGE SYSTEM v{APP_VERSION}\n")


def status():
    print(f"Storage device : {DEVICE} ({'present' if device_exists() else 'missing'})")
    print(f"Storage mounted: {'yes' if is_mounted() else 'no'}")
    print(f"File sharing   : {'online' if service_active() else 'offline'}")
    print(f"Disk space     : {disk_usage()}")
    print(f"Mount point    : {MOUNT_POINT}")
    print(f"Share name     : {SHARE_NAME}")
    print(f"Share user     : {SHARE_USER or 'not configured'}")
    if service_active():
        print_connection_info()


def protected(action):
    if verify_pin():
        return action()
    log("ACCESS DENIED")
    return False


def menu():
    actions = {
        "1": ("Start sharing files", lambda: protected(start_share)),
        "2": ("Stop sharing files", lambda: protected(stop_share)),
        "3": ("Connect storage drive", mount_storage),
        "4": ("Safely eject storage drive", unmount_storage),
        "5": ("Emergency lock", panic_lock),
        "6": ("Diagnostics", doctor),
        "7": ("Setup / change drive", setup),
        "8": ("Repair Samba share", repair_samba_share),
    }
    while state["running"]:
        clear(); banner(); status(); print("\nMENU")
        for key, (label, _) in actions.items(): print(f"  {key}) {label}")
        print("  9) Exit")
        choice = input("\nNAS > ").strip()
        if choice == "9":
            state["running"] = False
        elif choice in actions:
            actions[choice][1](); pause()
        else:
            log("Invalid option"); pause()


def parse_args():
    parser = argparse.ArgumentParser(description="Manage a removable-drive Samba NAS")
    parser.add_argument("--version", action="version", version=f"Nasberry {APP_VERSION}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show NAS status")
    sub.add_parser("online", help="mount storage and start sharing")
    sub.add_parser("offline", help="stop sharing and safely unmount")
    sub.add_parser("mount", help="mount storage")
    sub.add_parser("unmount", help="safely unmount storage")
    sub.add_parser("lock", help="immediately stop sharing and unmount")
    sub.add_parser("doctor", help="run diagnostics")
    sub.add_parser("repair-samba", help="recreate and validate the configured Samba share")
    setup_parser = sub.add_parser("setup", help="detect and configure a storage drive")
    setup_parser.add_argument("--non-interactive", action="store_true")
    setup_parser.add_argument("--skip-pin", action="store_true")
    safe = sub.add_parser("safe-mode", help="stop and disable configured Samba services")
    safe.add_argument("--yes", action="store_true", help="confirm this potentially disruptive action")
    return parser.parse_args()


def main():
    args = parse_args()
    if SAFE_MODE_ON_START:
        enforce_boot_safety()
    commands = {
        "status": lambda: (status() or True), "online": lambda: protected(start_share),
        "offline": lambda: protected(lambda: stop_share() and unmount_storage()), "mount": mount_storage,
        "unmount": unmount_storage, "lock": panic_lock, "doctor": doctor, "repair-samba": repair_samba_share,
    }
    if args.command == "setup":
        return setup(args.non_interactive, args.skip_pin)
    if args.command == "safe-mode":
        if not args.yes:
            log("Refusing to disable services without --yes")
            return False
        return enforce_boot_safety()
    if args.command in commands:
        return commands[args.command]()
    menu()
    return True


if __name__ == "__main__":
    try:
        raise SystemExit(0 if main() else 1)
    except KeyboardInterrupt:
        log("Interrupted; no additional changes were made")
        raise SystemExit(130)
