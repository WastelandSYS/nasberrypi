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
import termios
import tempfile
import textwrap
import tty
import time
from datetime import datetime
from pathlib import Path

APP_VERSION = "0.3.0"
DASHBOARD_STATUS_TTL = 1.0
DEFAULT_CONFIG_FILE = "/etc/nasberry/config.ini" if os.geteuid() == 0 else "~/.config/nasberry/config.ini"
CONFIG_FILE = Path(os.path.expanduser(os.environ.get("NASBERRY_CONFIG_FILE", DEFAULT_CONFIG_FILE)))
DEFAULT_SHARES_FILE = "/etc/nasberry/shares.json" if os.geteuid() == 0 else "~/.config/nasberry/shares.json"
SHARES_FILE = Path(os.path.expanduser(os.environ.get("NASBERRY_SHARES_FILE", DEFAULT_SHARES_FILE)))
NASBERRY_SAMBA_BEGIN = "# BEGIN NasberryPi managed shares"
NASBERRY_SAMBA_END = "# END NasberryPi managed shares"
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

def load_config(path):
    loaded = configparser.ConfigParser(interpolation=None)
    loaded["nasberry"] = DEFAULTS.copy()
    try:
        with path.open() as handle:
            loaded.read_file(handle)
    except FileNotFoundError:
        pass
    except (OSError, configparser.Error) as exc:
        print(f"WARNING: Could not read configuration {path}: {exc}", file=sys.stderr)
    return loaded


state = {"running": True}
config = load_config(CONFIG_FILE)
settings = config["nasberry"]


def setting(name, env_name=None):
    return os.environ.get(env_name or f"NASBERRY_{name.upper()}", settings.get(name, DEFAULTS[name]))


def refresh_settings():
    global DEVICE, MOUNT_POINT, SHARE_NAME, SHARE_USER, SAMBA_SERVICE, SAMBA_SERVICES, STATE_FILE, CHECK_DELAY, SAFE_MODE_ON_START
    DEVICE = setting("device")
    MOUNT_POINT = setting("mount_point")
    SHARE_NAME = setting("share_name")
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
    if msg.startswith("✔"):
        msg = styled(msg, "1", "32")
    elif msg.startswith("✖"):
        msg = styled(msg, "1", "31")
    elif msg.startswith("⚠"):
        msg = styled(msg, "1", "33")
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


def draw_screen(text):
    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J\033[H")
        sys.stdout.write(text)
        sys.stdout.flush()
    else:
        print(text)


def pause():
    input("\n  Press Enter to continue...")


def save_config():
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{CONFIG_FILE.name}.", dir=CONFIG_FILE.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            config.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        temp.replace(CONFIG_FILE)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


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

def device_mounted_at_nas():
    configured = os.path.realpath(MOUNT_POINT)
    return any(os.path.realpath(point) == configured for point in device_mount_points())

def active_mount_point():
    mounts = device_mount_points()
    if not mounts:
        return ""

    for point in mounts:
        if os.path.realpath(point) == os.path.realpath(MOUNT_POINT):
            return point

    return mounts[0]

def storage_mount_state():
    mount_point = active_mount_point()
    if not mount_point:
        return "safely_unmounted"
    if os.path.realpath(mount_point) == os.path.realpath(MOUNT_POINT):
        return "mounted_nas"
    return "mounted_elsewhere"

def storage_mount_state_label():
    return {
        "safely_unmounted": "safely unmounted",
        "mounted_nas": "mounted in NAS mode",
        "mounted_elsewhere": "mounted elsewhere",
    }[storage_mount_state()]

def ensure_mount_point():
    try:
        Path(MOUNT_POINT).mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        log(f"✖ Could not create mount point {MOUNT_POINT}: {exc}")
        return False


def device_exists():
    return os.path.exists(DEVICE)


def cleanup_other_mounts(confirm=True):
    mounts = [point for point in device_mount_points() if os.path.realpath(point) != os.path.realpath(MOUNT_POINT)]
    if mounts and confirm:
        log("⚠ Storage is mounted outside the Nasberry mount point.")
        log(f"Current mount point : {', '.join(mounts)}")
        log(f"Nasberry mount point: {MOUNT_POINT}")
        if input("Move it into NAS mode? [y/N]: ").strip().lower() not in {"y", "yes"}:
            log("Mount cancelled; storage remains mounted elsewhere")
            return False
    for point in mounts:
        log(f"Moving storage from {point} into NAS mode...")
        result = run(sudo_cmd("umount", point))
        if result.returncode != 0:
            log(f"✖ Could not unmount {point}: {result.stderr.strip() or 'device may be busy'}")
            return False
    return True


def mount_storage(repair_permissions=False):
    operation_header("MOUNT STORAGE", "Preparing storage for NAS access")
    if not ensure_mount_point() or not cleanup_other_mounts(confirm=not repair_permissions):
        write_state(is_mounted(), service_active())
        return False
    if is_mounted() and not device_mounted_at_nas():
        log(f"✖ Mount point is already occupied by a different filesystem: {MOUNT_POINT}")
        log("Unmount it or choose another Nasberry mount point before starting sharing.")
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
        log("✔ Storage is already mounted in NAS mode")
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
    mounted = result.returncode == 0 and device_mounted_at_nas()
    if mounted:
        log(f"✔ Storage mounted in NAS mode at {MOUNT_POINT}")
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
    operation_header("UNMOUNT STORAGE", "Safely disconnecting NAS storage")
    mount_point = active_mount_point()

    if not mount_point:
        log("✔ Storage is already unmounted")
        write_state(False, service_active())
        return True

    mounted_elsewhere = os.path.realpath(mount_point) != os.path.realpath(MOUNT_POINT)

    if mounted_elsewhere:
        log("⚠ Storage is mounted outside the Nasberry mount point")
        log(f"Current mount point : {mount_point}")
        log(f"Nasberry mount point: {MOUNT_POINT}")
        answer = input("Unmount this drive anyway? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            log("Unmount cancelled")
            write_state(True, service_active())
            return False

    if service_active() and not stop_share():
        log("✖ Refusing to unmount while the share is still active")
        return False

    log(f"Safely unmounting storage from {mount_point}...")
    result = run(sudo_cmd("umount", mount_point))
    time.sleep(CHECK_DELAY)

    still_mounted = bool(device_mount_points())
    unmounted = result.returncode == 0 and not still_mounted

    if unmounted:
        log("✔ Storage safely unmounted")
    else:
        log(f"✖ Unmount failed: {result.stderr.strip() or 'device may be busy'}")

    write_state(still_mounted, service_active())
    return unmounted


def public_share_path():
    return os.path.join(MOUNT_POINT, "Public")


def default_share():
    return {"name": "Public", "path": public_share_path(), "enabled": True, "read_only": False}


class ShareConfigError(Exception):
    pass


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def load_share_string(item, index, field, default=""):
    if field not in item:
        return default
    value = item[field]
    if isinstance(value, str):
        return value.strip()
    raise ShareConfigError(f"share {index}: {field} must be a string")


def load_share_bool(item, index, field, default):
    if field not in item or item[field] is None:
        return default
    value = item[field]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    elif isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ShareConfigError(f"share {index}: {field} must be a boolean")


def load_shares():
    try:
        text = SHARES_FILE.read_text()
    except FileNotFoundError as exc:
        raise ShareConfigError(f"share configuration file is missing: {SHARES_FILE}") from exc
    except OSError as exc:
        raise ShareConfigError(f"could not read share configuration {SHARES_FILE}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ShareConfigError(
            f"invalid JSON in {SHARES_FILE}: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc
    if isinstance(raw, dict):
        if "shares" not in raw:
            raise ShareConfigError("share configuration must contain a 'shares' list")
        items = raw["shares"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ShareConfigError("share configuration must be an object with a 'shares' list or a list of shares")
    if not isinstance(items, list):
        raise ShareConfigError("share configuration must contain a list of shares")
    shares = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ShareConfigError(f"share {index} is not an object")
        share = {
            "name": load_share_string(item, index, "name"),
            "path": load_share_string(item, index, "path"),
            "enabled": load_share_bool(item, index, "enabled", True),
            "read_only": load_share_bool(item, index, "read_only", False),
        }
        ok, reason = validate_share(share, shares)
        if not ok:
            raise ShareConfigError(f"share {index}: {reason}")
        shares.append(share)
    return shares


def ensure_default_shares_file():
    if not SHARES_FILE.exists():
        save_shares([default_share()])


def save_shares(shares):
    SHARES_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{SHARES_FILE.name}.", dir=SHARES_FILE.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"shares": shares}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        temp.replace(SHARES_FILE)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def share_path_from_input(value):
    value = value.strip()
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(MOUNT_POINT, value))


def validate_share(share, existing=None):
    name = str(share.get("name", "")).strip()
    if not name:
        return False, "share name cannot be empty"
    if any(character in name for character in "[]/\\#;=\n\r"):
        return False, "share name contains unsupported characters"
    raw_path = str(share.get("path") or name)
    if any(ord(character) < 32 or character == "\x7f" for character in raw_path):
        return False, "share path contains unsupported characters"
    existing = existing or []
    if any(item["name"].lower() == name.lower() for item in existing):
        return False, f"duplicate share name: {name}"
    path = share_path_from_input(raw_path)
    mount = Path(MOUNT_POINT).resolve(strict=False)
    absolute = Path(path).resolve(strict=False)
    if absolute == mount or mount not in absolute.parents:
        return False, f"share path must be under {MOUNT_POINT}"
    if ".." in Path(str(share.get("path", ""))).parts:
        return False, "share path must not contain .."
    current = mount
    for part in absolute.relative_to(mount).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            return False, "share path must not pass through a symbolic link"
    share["name"] = name
    share["path"] = str(absolute)
    return True, "ok"


def enabled_shares():
    return [share for share in load_shares() if share.get("enabled", True)]


def ensure_share_folders():
    if not SHARE_USER:
        log("✖ No Samba user is configured")
        return False
    if not device_mounted_at_nas():
        log("✖ Refusing to create shared folders while storage is unmounted")
        return False
    try:
        owner = pwd.getpwnam(SHARE_USER)
        shares = load_shares()
        for index, share in enumerate(shares):
            ok, reason = validate_share(share, shares[:index] + shares[index + 1:])
            if not ok:
                raise OSError(reason)
            folder = Path(share["path"])
            if folder.is_symlink():
                raise OSError(f"{share['name']} folder must not be a symbolic link")
            folder.mkdir(parents=True, exist_ok=True)
            mount_permissions = filesystem_uses_mount_permissions()
            if not mount_permissions:
                os.chown(folder, owner.pw_uid, owner.pw_gid)
                os.chmod(folder, 0o755 if share.get("read_only") else 0o775)
            entry = folder.stat()
            if entry.st_uid != owner.pw_uid:
                raise OSError(
                    f"{share['name']} folder is owned by UID {entry.st_uid}, expected {owner.pw_uid} ({SHARE_USER})"
                )
            if not share.get("read_only") and not (entry.st_mode & 0o200):
                raise OSError(f"{share['name']} folder is not writable by {SHARE_USER}")
        return True
    except (KeyError, OSError, ShareConfigError) as exc:
        log(f"✖ Could not prepare shared folders: {exc}")
        return False


def storage_folder_path(name):
    return os.path.join(MOUNT_POINT, name)


def ensure_storage_layout():
    if not SHARE_USER:
        log("✖ No Samba user is configured")
        return False
    if not device_mounted_at_nas():
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
    return ensure_share_folders()


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
    try:
        expected = {share["name"]: share for share in enabled_shares()}
    except ShareConfigError as exc:
        return False, str(exc)
    if not expected:
        return False, "no enabled Nasberry shares"
    for name, share in expected.items():
        configured = shares.get(name)
        if not configured:
            return False, f"share [{name}] was not found"
        configured_path = configured.get("path", "")
        if os.path.abspath(configured_path) != os.path.abspath(share["path"]):
            return False, f"share [{name}] points to {configured_path or 'no path'}, not {share['path']}"
        read_only = configured.get("read only", "no").lower() in {"yes", "true"}
        if read_only != bool(share.get("read_only")):
            return False, f"share [{name}] read-only setting does not match Nasberry config"
    return True, f"{len(expected)} enabled share(s) configured"


def start_share():
    operation_header("START SHARING", "Bringing Nasberry shared folders online")
    if not service_exists(SAMBA_SERVICE):
        log(f"✖ Samba service '{SAMBA_SERVICE}' was not found. Run 'nasberry doctor'.")
        write_state(is_mounted(), False)
        return False
    if not device_mounted_at_nas() and not mount_storage():
        log("✖ Refusing to start sharing without mounted storage")
        return False
    if not ensure_public_folder():
        log("✖ Refusing to start sharing without safe shared folders")
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
        log("✔ Sharing online")
        print_connection_info()
    else:
        log(f"✖ Failed to start sharing: {result.stderr.strip() or 'check systemctl status'}")
    write_state(is_mounted(), active)
    return active


def stop_share():
    operation_header("STOP SHARING", "Taking Nasberry shared folders offline")
    if not service_exists(SAMBA_SERVICE):
        write_state(is_mounted(), False)
        return True
    log("Stopping file sharing...")
    result = run(sudo_cmd("systemctl", "stop", SAMBA_SERVICE))
    time.sleep(1)
    stopped = result.returncode == 0 and not service_active()
    log("✔ Sharing offline" if stopped else f"✖ Failed to stop sharing: {result.stderr.strip()}")
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
    operation_header("EMERGENCY LOCK", "Stopping sharing and securing storage")
    log("⚠ Emergency lock activated")
    return stop_share() and unmount_storage()


def write_state(mounted, shared):
    mount_state = storage_mount_state()
    active = active_mount_point()
    try:
        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE_FILE).write_text(
            f"MOUNTED={int(mounted)}\nSHARED={int(shared)}\nDEVICE={DEVICE}\nMOUNT_POINT={MOUNT_POINT}\n"
            f"MOUNT_STATE={mount_state}\nACTIVE_MOUNT_POINT={active}\nNAS_MOUNT_POINT={MOUNT_POINT}\n"
            f"UPDATED_AT={datetime.now().isoformat(timespec='seconds')}\n"
        )
    except OSError as exc:
        log(f"⚠ Could not write state file: {exc}")


def disk_usage():
    mount_point = active_mount_point()

    if not mount_point:
        return "Not mounted"

    try:
        usage = shutil.disk_usage(mount_point)
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
        print(f"  Windows      : \\\\{address}\\{SHARE_NAME}")
        print(f"  macOS/Linux  : smb://{address}/{SHARE_NAME}")
        print(f"  Android/iOS  : smb://{address}/{SHARE_NAME}")


def check(label, ok, detail, fix="", label_width=0):
    symbol = "✔" if ok else "✖"
    symbol = styled(symbol, "1", "32" if ok else "31")
    label = f"{label:<{label_width}}" if label_width else label
    print(f"{symbol} {label}  {detail}")
    if not ok and fix:
        print(f"  {styled('Fix:', '1', '33')} {fix}")
    return ok


def section_header(title):
    print(f"\n{styled(title, '1', '36')}")


def operation_header(title, subtitle):
    if not state.get("dashboard_action"):
        print("\n".join(panel(title, [subtitle])))


def public_folder_access_valid():
    if not device_mounted_at_nas():
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


def share_folder_status(share):
    if not device_mounted_at_nas():
        return True, "not checked while storage is safely unmounted"
    ok, reason = validate_share(dict(share), [])
    if not ok:
        return False, reason
    folder = Path(share["path"])
    if folder.is_symlink():
        return False, "folder is a symbolic link"
    if not folder.is_dir():
        return False, f"missing: {folder}"
    if not SHARE_USER:
        return False, "no configured share user"
    try:
        owner = pwd.getpwnam(SHARE_USER)
        entry = folder.stat()
    except (KeyError, OSError) as exc:
        return False, str(exc)
    if entry.st_uid != owner.pw_uid and not filesystem_uses_mount_permissions():
        return False, f"owned by UID {entry.st_uid}, expected {owner.pw_uid} ({SHARE_USER})"
    return True, f"{'read-only' if share.get('read_only') else 'read-write'} at {folder}"


def protected_folder_status(name):
    if not device_mounted_at_nas():
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
    print("\nWindows connection help:")
    print(r"  1. Connect directly to \\IP\Public (replace IP with this Nasberry's address).")
    print("  2. If Windows cached an old Samba password, open Command Prompt and run:")
    print("     net use * /delete /y")
    print("  3. Reconnect to the Public share; stale shares may remain visible until Windows reconnects.")


def doctor():
    if not state.get("dashboard_action"):
        print("\n".join(panel("DIAGNOSTICS", ["Storage, Samba, and system health"])))
    results = []
    label_width = 27
    app_path = str(Path(__file__).resolve())
    section_header("SYSTEM")
    results.append(check("Application version", True, f"v{APP_VERSION} at {app_path}", label_width=label_width))
    results.append(check("Configuration", CONFIG_FILE.exists(), str(CONFIG_FILE), "Run 'sudo nasberry setup'.", label_width))
    results.append(check("Privileges", os.geteuid() == 0 or command_exists("sudo"), "root/sudo available" if os.geteuid() == 0 or command_exists("sudo") else "sudo unavailable", "Run Nasberry as root.", label_width))
    for command in ("mount", "umount", "lsblk", "systemctl", "smbd", "testparm", "ip", "smbpasswd", "pdbedit"):
        results.append(check(f"Required command: {command}", command_exists(command), shutil.which(command) or "missing", "Re-run install.sh.", label_width))
    section_header("STORAGE")
    results.append(check("Storage device", device_exists(), DEVICE, "Connect the drive or run 'sudo nasberry setup'.", label_width))
    results.append(check("Mount point", os.path.isdir(MOUNT_POINT), MOUNT_POINT, f"Create it with: sudo mkdir -p {MOUNT_POINT}", label_width))
    mount_state = storage_mount_state()
    active = active_mount_point()
    mount_details = {
        "safely_unmounted": "safely unmounted",
        "mounted_nas": f"mounted in NAS mode at {active}",
        "mounted_elsewhere": f"mounted elsewhere at {active}; configured Nasberry mount point: {MOUNT_POINT}",
    }
    results.append(check("Mount state", True, mount_details[mount_state], label_width=label_width))
    try:
        shares = load_shares()
        enabled = [share for share in shares if share.get("enabled", True)]
        results.append(check("Share configuration", bool(enabled), f"{len(enabled)} enabled of {len(shares)} configured", "Run 'sudo nasberry shares'.", label_width))
        for share in enabled:
            share_ok, share_detail = share_folder_status(share)
            results.append(check(f"[{share['name']}] folder", share_ok, share_detail, "Run 'sudo nasberry repair-samba' to repair it.", label_width))
    except ShareConfigError as exc:
        detail = str(exc)
        fix = "Run 'sudo nasberry setup'." if "missing" in detail else f"Inspect or repair {SHARES_FILE}."
        results.append(check("Share configuration", False, detail, fix, label_width))
    for folder_name in ("Private", "Backups"):
        protected_ok, protected_detail = protected_folder_status(folder_name)
        results.append(check(f"{folder_name} folder protection", protected_ok, protected_detail, "Run 'sudo nasberry repair-samba' to create or repair it.", label_width))
    section_header("SAMBA")
    results.append(check("Samba service", service_exists(SAMBA_SERVICE), SAMBA_SERVICE, "Install Samba or choose the correct service in setup.", label_width))
    valid, reason = samba_config_valid()
    results.append(check("Samba shares", valid, reason, "Run 'sudo nasberry repair-samba' to recreate it.", label_width))
    account_ok, account_detail = samba_account_valid()
    results.append(check("Samba account", account_ok, account_detail, f"Run 'sudo smbpasswd -a {SHARE_USER}' to create or reset it." if SHARE_USER else "Run 'sudo nasberry setup'.", label_width))
    section_header("NETWORK")
    print_connection_info()
    section_header("RESULT")
    passed = sum(results)
    symbol, color = ("✔", "32") if passed == len(results) else (("⚠", "33") if passed >= len(results) * 0.75 else ("✖", "31"))
    print(styled(f"{symbol} Result: {passed}/{len(results)} checks passed", "1", color))
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


def print_filesystem_guidance(filesystem):
    filesystem = (filesystem or "unknown").lower()
    print(f"\nSelected filesystem: {filesystem}")
    print("  ext4 is recommended for best reliability and Linux permissions.")
    if filesystem in {"exfat", "fat", "msdos", "ntfs", "ntfs3", "fuseblk", "vfat"}:
        print("  exFAT/NTFS may work for basic sharing but cannot enforce Linux folder permissions as reliably.")
    print("  Nasberry exports enabled shared folders over Samba; Public is created by default.")


def print_shares(shares=None):
    if shares is None:
        shares = load_shares()
    print("\nManaged shared folders:")
    if not shares:
        print("  (none configured)")
        return
    for share in shares:
        state_text = "enabled" if share.get("enabled", True) else "disabled"
        mode = "read-only" if share.get("read_only") else "read-write"
        print(f"  - {share['name']}: {state_text}, {mode}, {share['path']}")


def choose_share(shares):
    print_shares(shares)
    name = input("Share name: ").strip()
    return next((share for share in shares if share["name"].lower() == name.lower()), None)


def manage_shares():
    operation_header("MANAGE SHARED FOLDERS", f"Configuration: {SHARES_FILE}")
    if os.geteuid() != 0:
        log("✖ Shared folder management must run as root: sudo nasberry shares")
        return False
    try:
        shares = load_shares()
    except ShareConfigError as exc:
        log(f"✖ Share configuration error: {exc}")
        log("Inspect or repair the shares file, or run 'sudo nasberry setup' if this is a new install.")
        return False
    while True:
        print_shares(shares)
        print("\n  1) Create share")
        print("  2) Enable/disable share")
        print("  3) Set read-only/read-write")
        print("  4) Remove from Nasberry management")
        print("  5) Apply Samba configuration")
        print("  Q) Return")
        print("")
        choice = input("Select an action: ").strip().lower()
        if choice in {"q", ""}:
            return True
        if choice == "1":
            name = input("New share name: ").strip()
            path = share_path_from_input(input(f"Folder under {MOUNT_POINT} [{name}]: ").strip() or name)
            share = {"name": name, "path": path, "enabled": True, "read_only": False}
            ok, reason = validate_share(share, shares)
            if not ok:
                log(f"✖ {reason}")
                continue
            shares.append(share)
            save_shares(shares)
            log(f"✔ Added share [{share['name']}]")
        elif choice in {"2", "3", "4"}:
            share = choose_share(shares)
            if not share:
                log("✖ Share not found")
                continue
            if choice == "2":
                share["enabled"] = not share.get("enabled", True)
                save_shares(shares)
                log(f"✔ [{share['name']}] is now {'enabled' if share['enabled'] else 'disabled'}")
            elif choice == "3":
                share["read_only"] = not share.get("read_only", False)
                save_shares(shares)
                log(f"✔ [{share['name']}] is now {'read-only' if share['read_only'] else 'read-write'}")
            else:
                answer = input(f"Remove [{share['name']}] from Nasberry management? Data is not deleted. [y/N]: ").strip().lower()
                if answer in {"y", "yes"}:
                    shares.remove(share)
                    save_shares(shares)
                    log(f"✔ Removed [{share['name']}] from Nasberry management")
        elif choice == "5":
            if ensure_share_folders() and configure_samba_share() and restart_samba_service():
                log("✔ Samba configuration applied")
        else:
            log("✖ Unknown selection")


def samba_config_preflight():
    smb_file = Path("/etc/samba/smb.conf")
    missing = [command for command in ("smbd", "testparm") if not command_exists(command)]
    if missing:
        log(f"✖ Required Samba tools are missing: {', '.join(missing)}. Re-run install.sh to install Samba.")
        return False
    if not smb_file.is_file():
        log(f"✖ Samba configuration was not found at {smb_file}. Re-run install.sh to install Samba.")
        return False
    if not os.access(smb_file, os.R_OK | os.W_OK) or not os.access(smb_file.parent, os.W_OK):
        log(f"✖ Samba configuration is not writable/backuppable: {smb_file}")
        return False
    return True


def valid_share_user(share_user):
    return bool(share_user) and not any(
        character.isspace() or character in "[]#;=,"
        for character in share_user
    )


def setup_preflight(selected, share_user):
    failures = []
    if os.geteuid() != 0:
        failures.append("setup must run as root: sudo nasberry setup")
    for command in ("smbd", "testparm", "mount", "lsblk", "ip", "smbpasswd", "pdbedit"):
        if not command_exists(command):
            failures.append(f"required command is missing: {command}")

    device_path = selected.get("path") or selected.get("name") or ""
    if not device_path or not os.path.exists(device_path):
        failures.append(f"selected storage device does not exist: {device_path or 'unknown'}")
    if not selected.get("uuid"):
        failures.append(f"no UUID was found for selected storage device: {device_path or 'unknown'}")

    mount_path = Path(MOUNT_POINT)
    if mount_path.is_symlink() or (mount_path.exists() and not mount_path.is_dir()):
        failures.append(f"mount point is unsafe (must be a real directory): {MOUNT_POINT}")
    elif is_mounted():
        selected_mounts = [os.path.realpath(point) for point in device_mount_points(device_path)]
        if os.path.realpath(MOUNT_POINT) not in selected_mounts:
            failures.append(f"mount point is already busy with another device: {MOUNT_POINT}")
    elif mount_path.is_dir():
        try:
            if any(mount_path.iterdir()):
                failures.append(f"unmounted mount point is not empty: {MOUNT_POINT}")
        except OSError as exc:
            failures.append(f"mount point cannot be inspected safely: {exc}")

    if not valid_share_user(share_user):
        failures.append(f"Linux user is unsafe for Samba configuration: {share_user!r}")
    try:
        pwd.getpwnam(share_user)
    except (KeyError, TypeError):
        failures.append(f"Linux user does not exist: {share_user!r}")

    smb_file = Path("/etc/samba/smb.conf")
    if not smb_file.is_file():
        failures.append(f"Samba configuration was not found: {smb_file}")
    elif not os.access(smb_file, os.R_OK | os.W_OK) or not os.access(smb_file.parent, os.W_OK):
        failures.append(f"Samba configuration is not writable/backuppable: {smb_file}")

    if failures:
        log("✖ Setup preflight failed before any configuration or storage changes:")
        for failure in failures:
            log(f"  - {failure}")
        log("Re-run install.sh for missing Samba/tools, then run 'sudo nasberry doctor'.")
        return False
    log("✔ Setup preflight passed")
    return True


def restart_samba_service():
    if not service_exists(SAMBA_SERVICE):
        log(f"✖ Samba service '{SAMBA_SERVICE}' was not found. On Raspberry Pi OS/Debian, install the samba package and run 'sudo nasberry doctor'.")
        return False
    result = run(sudo_cmd("systemctl", "restart", SAMBA_SERVICE))
    if result.returncode != 0:
        log(f"✖ Samba restart failed: {result.stderr.strip()}")
        return False
    return True


def repair_samba_share():
    operation_header("REPAIR SAMBA", "Validating and restoring Nasberry shared folders")
    if os.geteuid() != 0:
        log("✖ Samba repair must run as root: sudo nasberry repair-samba")
        return False
    if not SHARE_USER:
        log("✖ No Samba user is configured. Run 'sudo nasberry setup' first.")
        return False
    if not valid_share_user(SHARE_USER):
        log("✖ Configured Samba user contains unsupported characters. Run 'sudo nasberry setup' again.")
        return False
    if not samba_config_preflight():
        return False
    if not mount_storage(repair_permissions=True) or not ensure_storage_layout() or not ensure_share_folders() or not configure_samba_share():
        log("✖ Samba repair failed. Review the validation error above.")
        return False
    if not restart_samba_service():
        return False
    log("✔ Samba shares repaired and validated")
    return True


def samba_share_block(share):
    return f"""[{share['name']}]
   path = {share['path']}
   browseable = yes
   available = yes
   read only = {'yes' if share.get('read_only') else 'no'}
   guest ok = no
   valid users = {SHARE_USER}
   force user = {SHARE_USER}
   follow symlinks = no
   wide links = no
   create mask = 0664
   directory mask = 0775
"""


def appliance_samba_config():
    blocks = "\n".join(samba_share_block(share) for share in enabled_shares())
    return f"{NASBERRY_SAMBA_BEGIN}\n# Managed by NasberryPi. Edit with 'sudo nasberry shares'.\n{blocks}{NASBERRY_SAMBA_END}\n"


def replace_managed_samba_section(text, section):
    start = text.find(NASBERRY_SAMBA_BEGIN)
    end = text.find(NASBERRY_SAMBA_END)
    if start != -1 and end != -1 and end > start:
        end += len(NASBERRY_SAMBA_END)
        return text[:start].rstrip() + "\n\n" + section.rstrip() + "\n" + text[end:].lstrip()
    return text.rstrip() + "\n\n" + section


def configure_samba_share():
    smb_file = Path("/etc/samba/smb.conf")
    if not valid_share_user(SHARE_USER):
        log("✖ Refusing to create Samba configuration with an unsafe share user")
        return False
    try:
        managed_section = appliance_samba_config()
    except ShareConfigError as exc:
        log(f"✖ Share configuration error: {exc}")
        return False
    if not samba_config_preflight():
        return False
    log("Updating the NasberryPi managed Samba section only.")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup = smb_file.with_name(f"{smb_file.name}.nasberry.{timestamp}.bak")
    shutil.copy2(smb_file, backup)
    log(f"Preserved previous Samba configuration at {backup}")
    descriptor, candidate_name = tempfile.mkstemp(prefix=f".{smb_file.name}.nasberry.", dir=smb_file.parent)
    candidate = Path(candidate_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(replace_managed_samba_section(smb_file.read_text(), managed_section))
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(smb_file, candidate)
        syntax = run(["testparm", "-s", str(candidate)])
        if syntax.returncode != 0:
            detail = syntax.stderr.strip() or syntax.stdout.strip() or "testparm rejected the candidate configuration"
            log(f"✖ Samba config validation failed: {detail}")
            return False
        os.replace(candidate, smb_file)
    finally:
        candidate.unlink(missing_ok=True)
    valid, reason = samba_config_valid()
    if valid:
        log("✔ Samba configured with Nasberry managed shares")
        return True
    shutil.copy2(backup, smb_file)
    log(f"✖ Samba config validation failed: {reason}")
    log("✔ Restored the previous Samba configuration")
    return False


def setup(non_interactive=False, skip_pin=False, share_user_arg=None):
    operation_header("SETUP", "Configuring Nasberry appliance storage and sharing")
    if os.geteuid() != 0:
        log("✖ Setup changes system files and must run as root: sudo nasberry setup")
        return False
    selected = choose_device(non_interactive)
    if not selected:
        return False
    if not non_interactive:
        default_user = os.environ.get("SUDO_USER") or getpass.getuser()
        share_user = input(f"Linux user allowed to access the share [{default_user}]: ").strip() or default_user
    else:
        share_user = share_user_arg or os.environ.get("NASBERRY_SHARE_USER") or settings.get("share_user")
    print_filesystem_guidance(selected.get("fstype"))
    if not setup_preflight(selected, share_user):
        return False
    settings["device"] = f"/dev/disk/by-uuid/{selected['uuid']}"
    settings["mount_point"] = MOUNT_POINT
    settings["share_name"] = "Public"
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
    ensure_default_shares_file()
    configured = mount_storage(repair_permissions=True) and ensure_storage_layout() and ensure_share_folders() and configure_samba_share()
    password_updated = False
    if configured and settings.get("share_user") and not non_interactive and command_exists("smbpasswd"):
        log(f"Set the Samba network password for {settings['share_user']}:")
        password_result = subprocess.run(["smbpasswd", "-a", settings["share_user"]], check=False)
        account_ok, account_detail = samba_account_valid() if password_result.returncode == 0 else (False, "smbpasswd failed")
        configured = password_result.returncode == 0 and account_ok
        password_updated = configured
        if not configured:
            log(f"✖ Samba password/account setup failed: {account_detail}")
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


def terminal_size():
    return shutil.get_terminal_size(fallback=(80, 24))


def terminal_width():
    columns = max(1, terminal_size().columns)
    width = min(columns, 100)
    if columns <= 100 and width > 1:
        width -= 1
    return max(1, width)


def terminal_height():
    return max(8, terminal_size().lines)


def fit_text(text, width):
    if width <= 0:
        return ""
    if width == 1 and len(text) > 1:
        return "…"
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


def color_enabled():
    return (
        sys.stdout.isatty()
        and os.environ.get("TERM", "dumb") != "dumb"
        and "NO_COLOR" not in os.environ
    )


def styled(text, *codes):
    if color_enabled() and codes:
        return f"\033[{';'.join(codes)}m{text}\033[0m"
    return text


def highlight(text):
    return styled(text, "1", "30", "46")


def panel(title, lines, width=None, selected=None, accent="36", wrap=True):
    width = width or terminal_width()
    inner = width - 4
    rule = "─" * (width - len(title) - 5)
    output = [styled(f"┌─ {title} {rule}┐", "1", accent)]
    for index, line in enumerate(lines):
        wrapped = (
            textwrap.wrap(str(line), inner, break_long_words=True, break_on_hyphens=False) or [""]
            if wrap
            else [fit_text(str(line), inner)]
        )
        for part in wrapped:
            content = f" {fit_text(part, inner):<{inner}} "
            left_edge = styled("│", accent)
            right_edge = styled("│", accent)
            output.append(f"{left_edge}{highlight(content) if index == selected else content}{right_edge}")
    output.append(styled(f"└{'─' * (width - 2)}┘", "1", accent))
    return output


def centered_line(text, width=None):
    width = width or terminal_width()
    return fit_text(text, width).center(width)


def centered(lines):
    width = terminal_width()
    indent = " " * max(0, (terminal_size().columns - width) // 2)
    return "\n".join(indent + line for line in lines)


def menu_mount_status():
    detected = device_mount_points()
    configured = os.path.realpath(MOUNT_POINT)
    active = next((point for point in detected if os.path.realpath(point) == configured), None)
    if active:
        return "● mounted in NAS mode", active
    if detected:
        return "● mounted elsewhere", ", ".join(detected)
    if is_mounted():
        return "● mounted in NAS mode", MOUNT_POINT
    return "○ safely unmounted", f"{MOUNT_POINT} (configured)"


def menu_status_lines():
    present = device_exists()
    mount_state, mount_location = menu_mount_status()
    sharing = service_active()
    try:
        share_detail = f"{len(enabled_shares())} enabled share(s)"
    except ShareConfigError:
        share_detail = "share config error"
    return [
        f"Storage      {'● present' if present else '○ missing'}   {mount_state}",
        f"Sharing      {'● sharing online' if sharing else '○ sharing offline'}   {share_detail}",
        f"Mount point  {mount_location}",
        f"Share user   {SHARE_USER or 'not configured'}",
        "Share mode   Multiple shared folders",
        f"Space        {disk_usage()}",
    ]


class DashboardStatusCache:
    def __init__(self, ttl=DASHBOARD_STATUS_TTL, clock=None):
        self.ttl = ttl
        self.clock = clock
        self.lines = None
        self.refreshed_at = None

    def get(self):
        now = self.clock() if self.clock else time.monotonic()
        if self.lines is None or self.refreshed_at is None or now - self.refreshed_at >= self.ttl:
            self.lines = menu_status_lines()
            self.refreshed_at = now
        return self.lines

    def invalidate(self):
        self.lines = None
        self.refreshed_at = None


def compact_menu(actions, selected, width, height):
    tiny = height <= len(actions) + 3
    lines = [styled(centered_line(f"NASBERRY v{APP_VERSION}", width), "1", "36")]
    if not tiny:
        lines.append(styled(centered_line("NETWORK STORAGE CONSOLE", width), "2", "37"))
    for index, (shortcut, (label, _)) in enumerate(actions.items()):
        marker = "❯" if index == selected else " "
        lines.append(f"{marker} {shortcut} {fit_text(label, width - 4)}")
    marker = "❯" if selected == len(actions) else " "
    lines.append(f"{marker} Q Exit")
    help_text = "↑/↓ Navigate  Enter Select  1–9 Shortcut  Q Exit"
    if tiny:
        help_text = "↑/↓ Enter  1–9  Q"
    lines.append(centered_line(help_text, width))
    return centered(lines)


def menu_frame(actions, selected, width, status_lines, compact=False):
    lines = [
        styled(centered_line("NASBERRY", width), "1", "36"),
        styled(centered_line(f"VERSION {APP_VERSION}", width), "1", "35"),
        styled(centered_line("◆  NETWORK STORAGE CONSOLE  ◆", width), "2", "37"),
    ]
    if not compact:
        lines.append("")
    lines.extend(panel("SYSTEM STATUS", status_lines, width, accent="34", wrap=not compact))
    if not compact:
        lines.append("")
    menu_lines = []
    for index, (shortcut, (label, _)) in enumerate(actions.items()):
        marker = "❯" if index == selected else " "
        menu_lines.append(f"{marker}  {shortcut}   {label}")
    menu_lines.append("   Q   Exit")
    lines.extend(panel("MAIN MENU", menu_lines, width, selected, accent="36", wrap=False))
    if not compact:
        lines.append("")
    help_text = "↑/↓ Navigate  •  Enter Select  •  1–9 Shortcut  •  Q Exit"
    lines.append(styled(centered_line(help_text, width), "2", "37"))
    return centered(lines)


def render_menu(actions, selected=0, status_lines=None):
    width = terminal_width()
    height = terminal_height()
    if width < 40 or height < len(actions) + 6:
        return compact_menu(actions, selected, width, height)

    status_lines = status_lines if status_lines is not None else menu_status_lines()
    full = menu_frame(actions, selected, width, status_lines, compact=False)
    if len(full.splitlines()) <= height:
        return full
    compact = menu_frame(actions, selected, width, status_lines, compact=True)
    if len(compact.splitlines()) <= height:
        return compact
    return compact_menu(actions, selected, width, height)


def read_menu_key():
    if not sys.stdin.isatty():
        return input("\n  Select an action [1-9]: ").strip().lower()
    key = sys.stdin.read(1)
    if key == "\x1b":
        key += sys.stdin.read(2)
    return key.lower()


class DashboardTerminal:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self.enabled = self.stream.isatty()
        self.descriptor = self.stream.fileno() if self.enabled else None
        self.original = termios.tcgetattr(self.descriptor) if self.enabled else None
        self.navigation_active = False

    def enable_navigation_mode(self):
        if self.enabled and not self.navigation_active:
            tty.setcbreak(self.descriptor, termios.TCSANOW)
            self.navigation_active = True

    def restore_normal_mode(self, flush_input=False):
        if self.enabled and self.original is not None and self.navigation_active:
            when = termios.TCSAFLUSH if flush_input else termios.TCSADRAIN
            termios.tcsetattr(self.descriptor, when, self.original)
            self.navigation_active = False

    def __enter__(self):
        self.enable_navigation_mode()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.restore_normal_mode(flush_input=True)
        return False


def show_action_feedback(label, mode="action"):
    messages = {
        "action": "Executing requested operation. Status updates will appear below.",
        "prompt": "Complete the prompts below, then return to the dashboard.",
        "report": "Review the report below, then return to the dashboard.",
    }
    clear()
    print(centered(panel("NASBERRY", [label, messages.get(mode, messages["action"])])))


def show_menu_exit():
    clear()
    print(centered(panel("NASBERRY", ["Dashboard closed safely.", "No shutdown or unmount action was run."])))
    print()


def banner():
    print(f"\nNASBERRY NETWORK STORAGE SYSTEM v{APP_VERSION}\n")


def status():
    print(f"Storage device : {DEVICE} ({'present' if device_exists() else 'missing'})")
    mount_point = active_mount_point()
    print(f"Mount state    : {storage_mount_state_label()}")
    print(f"File sharing   : {'sharing online' if service_active() else 'sharing offline'}")
    print("Share mode     : Multiple shared folders")
    try:
        share_count = str(len(enabled_shares()))
    except ShareConfigError as exc:
        share_count = f"configuration error: {exc}"
    print(f"Enabled shares : {share_count}")
    print(f"Disk space     : {disk_usage()}")
    print(f"Mount point    : {mount_point or MOUNT_POINT}")
    print(f"Share user     : {SHARE_USER or 'not configured'}")
    if service_active():
        print_connection_info()

def storage_info():
    active = active_mount_point()
    print(f"Storage device      : {DEVICE}")
    print(f"Device              : {'present' if device_exists() else 'missing'}")
    print(f"Filesystem          : {storage_filesystem() or 'unknown'}")
    print(f"Mount state         : {storage_mount_state_label()}")
    print(f"Active mount point  : {active or '-'}")
    print(f"Nasberry mount point: {MOUNT_POINT}")
    print(f"Disk space          : {disk_usage()}")


def protected(action):
    if verify_pin():
        return action()
    log("ACCESS DENIED")
    return False


def run_dashboard_action(action):
    state["dashboard_action"] = True
    try:
        return action()
    finally:
        state.pop("dashboard_action", None)


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
        "9": ("Manage shared folders", manage_shares),
    }

    action_modes = {
        "1": "action",
        "2": "action",
        "3": "action",
        "4": "action",
        "5": "action",
        "6": "report",
        "7": "prompt",
        "8": "prompt",
        "9": "prompt",
    }

    selected = 0
    action_keys = list(actions)
    clean_exit = False
    terminal = DashboardTerminal()
    status_cache = DashboardStatusCache()

    def run_selected_action(key):
        label, action = actions[key]
        terminal.restore_normal_mode(flush_input=True)
        show_action_feedback(label, action_modes.get(key, "action"))
        run_dashboard_action(action)
        status_cache.invalidate()
        pause()
        clear()
        terminal.enable_navigation_mode()

    try:
        clear()
        with terminal:
            while state["running"]:
                draw_screen(render_menu(actions, selected, status_cache.get()))
                choice = read_menu_key()
                if choice in {"q", "\x03"}:
                    clean_exit = True
                    state["running"] = False
                elif choice in {"\x1b[a", "k", "w"}:
                    selected = (selected - 1) % (len(actions) + 1)
                elif choice in {"\x1b[b", "j", "s"}:
                    selected = (selected + 1) % (len(actions) + 1)
                elif choice in {"\r", "\n"}:
                    if selected == len(actions):
                        clean_exit = True
                        state["running"] = False
                    else:
                        run_selected_action(action_keys[selected])
                elif choice in actions:
                    selected = action_keys.index(choice)
                    run_selected_action(choice)
    except KeyboardInterrupt:
        clean_exit = True
        state["running"] = False
    finally:
        terminal.restore_normal_mode(flush_input=True)
        if clean_exit:
            show_menu_exit()


def parse_args():
    parser = argparse.ArgumentParser(description="Manage a removable-drive Samba NAS")
    parser.add_argument("--version", action="version", version=f"Nasberry {APP_VERSION}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show NAS status")
    sub.add_parser("storage", help="show storage status")
    sub.add_parser("online", help="mount storage and start sharing")
    sub.add_parser("offline", help="stop sharing and safely unmount")
    sub.add_parser("mount", help="mount storage")
    sub.add_parser("unmount", help="safely unmount storage")
    sub.add_parser("lock", help="immediately stop sharing and unmount")
    sub.add_parser("doctor", help="run diagnostics")
    sub.add_parser("repair-samba", help="recreate and validate the configured Samba share")
    sub.add_parser("shares", help="manage Nasberry shared folders")
    setup_parser = sub.add_parser("setup", help="detect and configure a storage drive")
    setup_parser.add_argument("--non-interactive", action="store_true")
    setup_parser.add_argument("--skip-pin", action="store_true")
    setup_parser.add_argument("--share-user", help="Linux user allowed to access shares during non-interactive setup")
    safe = sub.add_parser("safe-mode", help="stop and disable configured Samba services")
    safe.add_argument("--yes", action="store_true", help="confirm this potentially disruptive action")
    return parser.parse_args()


def main():
    args = parse_args()
    if SAFE_MODE_ON_START:
        enforce_boot_safety()
    commands = {
        "status": lambda: (status() or True), "storage": lambda: (storage_info() or True), "online": lambda: protected(start_share),
        "offline": lambda: protected(lambda: stop_share() and unmount_storage()), "mount": mount_storage,
        "unmount": unmount_storage, "lock": panic_lock, "doctor": doctor, "repair-samba": repair_samba_share,
        "shares": manage_shares,
    }
    if args.command == "setup":
        return setup(args.non_interactive, args.skip_pin, args.share_user)
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
