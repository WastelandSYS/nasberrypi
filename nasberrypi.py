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

APP_VERSION = "0.4.0"
DASHBOARD_STATUS_TTL = 1.0
DEFAULT_CONFIG_FILE = "/etc/nasberry/config.ini" if os.geteuid() == 0 else "~/.config/nasberry/config.ini"
CONFIG_FILE = Path(os.path.expanduser(os.environ.get("NASBERRY_CONFIG_FILE", DEFAULT_CONFIG_FILE)))
DEFAULT_SHARES_FILE = "/etc/nasberry/shares.json" if os.geteuid() == 0 else "~/.config/nasberry/shares.json"
SHARES_FILE = Path(os.path.expanduser(os.environ.get("NASBERRY_SHARES_FILE", DEFAULT_SHARES_FILE)))
NASBERRY_SAMBA_BEGIN = "# BEGIN NasberryPi managed shares"
NASBERRY_SAMBA_END = "# END NasberryPi managed shares"
LEGACY_SAMBA_SHARE_PREFIX = "# Managed by Nasberry: "
LEGACY_APPLIANCE_HEADER = "# Managed by Nasberry appliance mode. Previous config is saved before replacement."
LEGACY_APPLIANCE_MARKER = "# Managed by Nasberry appliance mode"
LEGACY_APPLIANCE_BEGIN = "# BEGIN Managed by Nasberry appliance mode"
LEGACY_APPLIANCE_END = "# END Managed by Nasberry appliance mode"
LEGACY_APPLIANCE_DISABLE_COMMENT = "# Nasberry appliance mode: disable share"
LEGACY_APPLIANCE_USER_SHARE_LIMIT = "usershare max shares = 0"
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


class ConfigError(Exception):
    pass


def default_config():
    loaded = configparser.ConfigParser(interpolation=None)
    loaded["nasberry"] = DEFAULTS.copy()
    return loaded


def has_control_character(value):
    return any(ord(character) < 32 or character == "\x7f" for character in value)


def parse_config_bool(value, name):
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid {name} value: {value!r}")


def parse_config_delay(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"invalid check_delay value: {value!r}") from exc
    if parsed < 0:
        raise ConfigError("invalid check_delay value: must be >= 0")
    return parsed


def validate_path_value(value, name, absolute=False, reject_root=False):
    if not value or has_control_character(value):
        raise ConfigError(f"invalid {name} value")
    if absolute and not os.path.isabs(value):
        raise ConfigError(f"invalid {name} value: must be an absolute path")
    if reject_root and os.path.abspath(value) == os.path.sep:
        raise ConfigError(f"invalid {name} value: must not be /")
    return value


def valid_share_user_value(value):
    return bool(value) and not any(
        character.isspace() or character in "[]#;=,"
        for character in value
    )


def valid_service_token(value):
    return bool(value) and not has_control_character(value) and all(
        character.isalnum() or character in "_.@:-"
        for character in value
    )


def validate_config_section(section, path):
    for name in section:
        value = section[name]
        if name == "check_delay":
            parse_config_delay(value)
        elif name == "safe_mode_on_start":
            parse_config_bool(value, name)
        elif name == "mount_point":
            validate_path_value(value, name, absolute=True, reject_root=True)
        elif name == "device":
            validate_path_value(value, name)
        elif name == "share_user":
            if value and not valid_share_user_value(value):
                raise ConfigError(f"invalid share_user value in {path}")
        elif name == "samba_service":
            if not valid_service_token(value):
                raise ConfigError(f"invalid samba_service value in {path}")
        elif name == "samba_services":
            services = [item.strip() for item in value.split(",")]
            if not services or not any(services):
                raise ConfigError(f"invalid samba_services value in {path}")
            for service in services:
                if not valid_service_token(service):
                    raise ConfigError(f"invalid samba_services value in {path}")
        elif name == "state_file":
            validate_path_value(value, name)
        elif name == "share_name":
            if has_control_character(value):
                raise ConfigError(f"invalid share_name value in {path}")


def merge_config_defaults(parsed):
    loaded = configparser.ConfigParser(interpolation=None)
    for section in parsed.sections():
        loaded.add_section(section)
        for key, value in parsed.items(section, raw=True):
            loaded[section][key] = value
    for key, value in DEFAULTS.items():
        if key not in loaded["nasberry"]:
            loaded["nasberry"][key] = value
    return loaded


def load_config_with_status(path):
    parsed = configparser.ConfigParser(interpolation=None)
    try:
        with path.open() as handle:
            parsed.read_file(handle)
    except FileNotFoundError:
        return default_config(), "missing", f"not configured: {path}"
    except (OSError, configparser.Error) as exc:
        return default_config(), "invalid", f"invalid configuration {path}: {exc}"
    if not parsed.has_section("nasberry"):
        return default_config(), "invalid", f"configuration {path} is missing [nasberry]"
    try:
        validate_config_section(parsed["nasberry"], path)
        loaded = merge_config_defaults(parsed)
    except ConfigError as exc:
        return default_config(), "invalid", f"invalid configuration {path}: {exc}"
    return loaded, "valid", None


def load_config(path):
    return load_config_with_status(path)[0]


state = {"running": True}
config, CONFIG_STATUS, CONFIG_ERROR = load_config_with_status(CONFIG_FILE)
settings = config["nasberry"]
CONFIG_RUNTIME_ERROR = None


def publish_config_from_disk():
    global config, settings, CONFIG_STATUS, CONFIG_ERROR
    config, CONFIG_STATUS, CONFIG_ERROR = load_config_with_status(CONFIG_FILE)
    settings = config["nasberry"]
    refresh_settings()
    return config_ready()


def setting(name, env_name=None):
    return os.environ.get(env_name or f"NASBERRY_{name.upper()}", settings.get(name, DEFAULTS[name]))


def apply_default_runtime_settings():
    global DEVICE, MOUNT_POINT, SHARE_NAME, SHARE_USER, SAMBA_SERVICE, SAMBA_SERVICES, STATE_FILE, CHECK_DELAY, SAFE_MODE_ON_START
    DEVICE = DEFAULTS["device"]
    MOUNT_POINT = DEFAULTS["mount_point"]
    SHARE_NAME = DEFAULTS["share_name"]
    SHARE_USER = DEFAULTS["share_user"]
    SAMBA_SERVICE = DEFAULTS["samba_service"]
    SAMBA_SERVICES = [item.strip() for item in DEFAULTS["samba_services"].split(",") if item.strip()]
    STATE_FILE = os.path.expanduser(DEFAULTS["state_file"])
    CHECK_DELAY = parse_config_delay(DEFAULTS["check_delay"])
    SAFE_MODE_ON_START = parse_config_bool(DEFAULTS["safe_mode_on_start"], "safe_mode_on_start")


def refresh_settings():
    global DEVICE, MOUNT_POINT, SHARE_NAME, SHARE_USER, SAMBA_SERVICE, SAMBA_SERVICES, STATE_FILE, CHECK_DELAY, SAFE_MODE_ON_START, CONFIG_RUNTIME_ERROR
    try:
        DEVICE = validate_path_value(setting("device"), "device")
        MOUNT_POINT = validate_path_value(setting("mount_point"), "mount_point", absolute=True, reject_root=True)
        SHARE_NAME = setting("share_name")
        if has_control_character(SHARE_NAME):
            raise ConfigError("invalid share_name value")
        SHARE_USER = setting("share_user")
        if SHARE_USER and not valid_share_user_value(SHARE_USER):
            raise ConfigError("invalid share_user value")
        SAMBA_SERVICE = setting("samba_service")
        if not valid_service_token(SAMBA_SERVICE):
            raise ConfigError("invalid samba_service value")
        raw_services = setting("samba_services")
        SAMBA_SERVICES = [item.strip() for item in raw_services.split(",") if item.strip()]
        if not SAMBA_SERVICES or not all(valid_service_token(item) for item in SAMBA_SERVICES):
            raise ConfigError("invalid samba_services value")
        STATE_FILE = os.path.expanduser(validate_path_value(setting("state_file"), "state_file"))
        CHECK_DELAY = parse_config_delay(setting("check_delay"))
        SAFE_MODE_ON_START = parse_config_bool(setting("safe_mode_on_start"), "safe_mode_on_start")
        CONFIG_RUNTIME_ERROR = None
    except ConfigError as exc:
        CONFIG_RUNTIME_ERROR = f"invalid runtime configuration override: {exc}"
        apply_default_runtime_settings()


refresh_settings()


def log(msg):
    if msg.startswith("✔"):
        msg = styled(msg, "1", "32")
    elif msg.startswith("✖"):
        msg = styled(msg, "1", "31")
    elif msg.startswith("⚠"):
        msg = styled(msg, "1", "33")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def config_ready():
    return CONFIG_STATUS == "valid" and CONFIG_RUNTIME_ERROR is None


def config_problem():
    if CONFIG_RUNTIME_ERROR:
        return CONFIG_RUNTIME_ERROR, "Inspect the NASBERRY_* environment overrides."
    if CONFIG_STATUS == "missing":
        return f"Nasberry has not been configured: {CONFIG_FILE}", "Run 'sudo nasberry setup'."
    if CONFIG_STATUS == "invalid":
        return CONFIG_ERROR or f"invalid configuration: {CONFIG_FILE}", f"Inspect or repair {CONFIG_FILE}."
    return "configuration is valid", ""


def require_valid_config(operation=None):
    if config_ready():
        return True
    detail, fix = config_problem()
    action = f" for {operation}" if operation else ""
    log(f"✖ Configuration unavailable{action}: {detail}")
    if fix:
        log(f"  {fix}")
    return False


def configuration_check():
    if config_ready():
        return True, str(CONFIG_FILE), ""
    detail, fix = config_problem()
    return False, detail, fix


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
    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J\033[H")
        sys.stdout.flush()


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
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
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
    if not filesystem_uses_mount_permissions():
        return []
    if not SHARE_USER:
        return None
    try:
        owner = pwd.getpwnam(SHARE_USER)
    except (KeyError, TypeError):
        return None
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
        if not sys.stdin.isatty():
            log("✖ Confirmation is required before moving externally mounted storage.")
            log("Run this command interactively to continue.")
            return False
        try:
            answer = input("Move it into NAS mode? [y/N]: ").strip().lower()
        except EOFError:
            log("✖ Confirmation is required before moving externally mounted storage.")
            return False
        if answer not in {"y", "yes"}:
            log("Mount cancelled; storage remains mounted elsewhere")
            return False
    for point in mounts:
        log(f"Moving storage from {point} into NAS mode...")
        result = run(sudo_cmd("umount", point))
        if result.returncode != 0:
            log(f"✖ Could not unmount {point}: {result.stderr.strip() or 'device may be busy'}")
            return False
    return True


def mount_storage(repair_permissions=False, confirm_external_move=True):
    operation_header("MOUNT STORAGE", "Preparing storage for NAS access")
    stopped_share_for_repair = False
    options = storage_mount_options()
    if options is None:
        if SHARE_USER:
            log(f"✖ Cannot mount this filesystem safely because the configured Linux share user does not exist: {SHARE_USER!r}")
        else:
            log("✖ Cannot mount this filesystem safely because no Linux share user is configured.")
        log("This filesystem requires mount ownership options.")
        log("Run 'sudo nasberry setup' to select a valid share user.")
        return False
    if not ensure_mount_point():
        write_state(is_mounted(), service_active())
        return False
    if is_mounted() and not device_mounted_at_nas():
        log(f"✖ Mount point is already occupied by a different filesystem: {MOUNT_POINT}")
        log("Unmount it or choose another Nasberry mount point before starting sharing.")
        write_state(False, service_active())
        return False
    if not cleanup_other_mounts(confirm=confirm_external_move):
        write_state(is_mounted(), service_active())
        return False
    if is_mounted() and repair_permissions and options:
        if service_active():
            if not stop_share():
                log("✖ Could not stop sharing before repairing storage permissions")
                return False
            stopped_share_for_repair = True
        result = run(sudo_cmd("umount", MOUNT_POINT))
        if result.returncode != 0:
            log(f"✖ Could not remount storage: {result.stderr.strip() or 'device may be busy'}")
            restore_share_after_failed_remount(stopped_share_for_repair)
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
        if stopped_share_for_repair:
            restore_share_after_failed_remount(stopped_share_for_repair)
    actual_mounted = device_mounted_at_nas() if stopped_share_for_repair else mounted
    write_state(actual_mounted, service_active())
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
        if not sys.stdin.isatty():
            log("✖ Confirmation is required before unmounting externally mounted storage.")
            log("Run this command interactively to continue.")
            return False
        try:
            answer = input("Unmount this drive anyway? [y/N]: ").strip().lower()
        except EOFError:
            log("✖ Confirmation is required before unmounting externally mounted storage.")
            return False
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


class SambaConfigError(Exception):
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


def load_shares(check_filesystem=True):
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
        ok, reason = validate_share(share, shares, check_filesystem=check_filesystem)
        if not ok:
            raise ShareConfigError(f"share {index}: {reason}")
        shares.append(share)
    return shares


def ensure_default_shares_file():
    if os.path.lexists(SHARES_FILE):
        return True
    try:
        save_shares([default_share()])
    except OSError as exc:
        log(f"✖ Could not create share configuration {SHARES_FILE}: {exc}")
        return False
    return True


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
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def share_path_from_input(value):
    value = value.strip()
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(MOUNT_POINT, value))


def validate_share(share, existing=None, check_filesystem=True):
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
    if ".." in Path(str(share.get("path", ""))).parts:
        return False, "share path must not contain .."
    path = share_path_from_input(raw_path)
    if check_filesystem:
        mount = Path(MOUNT_POINT).resolve(strict=False)
        absolute = Path(path).resolve(strict=False)
        if absolute == mount or mount not in absolute.parents:
            return False, f"share path must be under {MOUNT_POINT}"
        current = mount
        for part in absolute.relative_to(mount).parts:
            current = current / part
            if current.exists() and current.is_symlink():
                return False, "share path must not pass through a symbolic link"
        normalized_path = str(absolute)
    else:
        mount = os.path.abspath(os.path.normpath(MOUNT_POINT))
        absolute = os.path.abspath(os.path.normpath(path))
        try:
            common = os.path.commonpath([mount, absolute])
        except ValueError:
            return False, f"share path must be under {MOUNT_POINT}"
        if absolute == mount or common != mount:
            return False, f"share path must be under {MOUNT_POINT}"
        normalized_path = absolute
    share["name"] = name
    share["path"] = normalized_path
    return True, "ok"


def enabled_shares():
    return [share for share in load_shares() if share.get("enabled", True)]


def nasberry_sharing_online(enabled=None):
    if enabled is None:
        enabled = enabled_shares()
    return bool(enabled) and device_mounted_at_nas() and service_active()


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


def managed_samba_share_names(text=None):
    if text is None:
        smb_file = Path("/etc/samba/smb.conf")
        try:
            text = smb_file.read_text()
        except OSError as exc:
            return False, [], f"could not read Samba configuration {smb_file}: {exc}"
    lines = text.splitlines()
    begins = [index for index, line in enumerate(lines) if line.strip() == NASBERRY_SAMBA_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == NASBERRY_SAMBA_END]
    if not begins and not ends:
        return False, [], "Nasberry managed Samba section was not found"
    if len(begins) != 1 or len(ends) != 1:
        return False, [], "Nasberry managed Samba section markers are malformed"
    begin, end = begins[0], ends[0]
    if begin >= end:
        return False, [], "Nasberry managed Samba section markers are out of order"
    names = []
    seen = set()
    for raw_line in lines[begin + 1:end]:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            key = name.lower()
            if key in seen:
                return False, [], f"duplicate Nasberry managed Samba share: {name}"
            seen.add(key)
            names.append(name)
    return True, names, "ok"


def samba_section_name(line):
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return None


def samba_section_blocks(lines, start=0, end=None):
    end = len(lines) if end is None else end
    blocks = []
    current = None
    for index in range(start, end):
        name = samba_section_name(lines[index])
        if name is not None:
            if current is not None:
                current["end"] = index
                blocks.append(current)
            current = {"name": name, "start": index, "end": end, "options": {}}
        elif current is not None and "=" in lines[index]:
            key, value = lines[index].split("=", 1)
            current["options"][key.strip().lower()] = value.strip()
    if current is not None:
        blocks.append(current)
    return blocks


def current_managed_marker_bounds(lines):
    begins = [index for index, line in enumerate(lines) if line.strip() == NASBERRY_SAMBA_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == NASBERRY_SAMBA_END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1:
        raise SambaConfigError("Nasberry managed Samba section markers are malformed")
    begin, end = begins[0], ends[0]
    if begin >= end:
        raise SambaConfigError("Nasberry managed Samba section markers are out of order")
    return begin, end


def legacy_appliance_marker_bounds(lines):
    begins = [index for index, line in enumerate(lines) if line.strip() == LEGACY_APPLIANCE_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == LEGACY_APPLIANCE_END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1:
        raise SambaConfigError("legacy Nasberry appliance markers are malformed")
    begin, end = begins[0], ends[0]
    if begin >= end:
        raise SambaConfigError("legacy Nasberry appliance markers are out of order")
    return begin, end


def legacy_share_marker_name(line):
    stripped = line.strip()
    if stripped.startswith(LEGACY_SAMBA_SHARE_PREFIX):
        return stripped[len(LEGACY_SAMBA_SHARE_PREFIX):].strip()
    return None


def marked_legacy_share_end(lines, marker_index, share_name):
    index = marker_index + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    section_name = samba_section_name(lines[index]) if index < len(lines) else None
    if not section_name or section_name.lower() != share_name.lower():
        return None
    end = index + 1
    while end < len(lines) and samba_section_name(lines[end]) is None:
        end += 1
    return end


def skip_samba_section(lines, index):
    end = index + 1
    while end < len(lines) and samba_section_name(lines[end]) is None:
        end += 1
    return end


def bounds_contain(bounds, index):
    return bounds is not None and bounds[0] <= index <= bounds[1]


def sections_outside_current(lines, current_bounds):
    return [
        section
        for section in samba_section_blocks(lines)
        if not bounds_contain(current_bounds, section["start"])
    ]


def legacy_appliance_public_bounds(lines, current_bounds):
    public_sections = [
        section
        for section in sections_outside_current(lines, current_bounds)
        if section["name"].lower() == "public"
    ]
    default_sections = [section for section in public_sections if section_is_default_public(section)]
    if len(public_sections) > 1 or (public_sections and len(default_sections) != 1):
        raise SambaConfigError("legacy Nasberry appliance Public share is ambiguous")
    if len(default_sections) == 1:
        section = default_sections[0]
        end = section["end"]
        if current_bounds is not None and section["start"] < current_bounds[0] < end:
            end = current_bounds[0]
        return section["start"], end
    return None


def remove_legacy_nasberry_samba_content(text):
    lines = text.splitlines(keepends=True)
    current_bounds = current_managed_marker_bounds(lines)
    legacy_bounds = legacy_appliance_marker_bounds(lines)
    legacy_appliance_file = any(line.strip() == LEGACY_APPLIANCE_HEADER for line in lines)
    legacy_public_bounds = legacy_appliance_public_bounds(lines, current_bounds) if legacy_appliance_file else None
    output = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if legacy_bounds is not None and index == legacy_bounds[0]:
            index = legacy_bounds[1] + 1
            continue
        marker_name = legacy_share_marker_name(lines[index])
        if marker_name is not None:
            end = marked_legacy_share_end(lines, index, marker_name)
            if end is None:
                detail = marker_name or "<empty>"
                raise SambaConfigError(f"legacy Nasberry share marker is ambiguous: {detail}")
            index = end
            continue
        if stripped == LEGACY_APPLIANCE_DISABLE_COMMENT:
            if index + 1 >= len(lines) or lines[index + 1].strip().lower() != "available = no":
                raise SambaConfigError("legacy Nasberry appliance disable marker is ambiguous")
            index += 2
            continue
        if stripped == LEGACY_APPLIANCE_HEADER:
            index += 1
            continue
        if stripped == LEGACY_APPLIANCE_MARKER:
            raise SambaConfigError("standalone legacy Nasberry appliance marker is ambiguous")
        if legacy_appliance_file and stripped.lower() == LEGACY_APPLIANCE_USER_SHARE_LIMIT:
            index += 1
            continue
        if legacy_public_bounds is not None and index == legacy_public_bounds[0]:
            index = legacy_public_bounds[1]
            continue
        output.append(lines[index])
        index += 1
    return "".join(output)


def section_is_default_public(section):
    if section["name"].lower() != "public":
        return False
    path = section["options"].get("path", "")
    if os.path.abspath(path) != os.path.abspath(public_share_path()):
        return False
    read_only = section["options"].get("read only", "no").lower() in {"yes", "true"}
    return not read_only


def sections_contain_only_default_public(sections, allow_homes=False):
    exported = [section for section in sections if section["name"].lower() not in {"global"}]
    if allow_homes:
        exported = [section for section in exported if section["name"].lower() != "homes"]
    return len(exported) == 1 and section_is_default_public(exported[0])


def sections_contain_no_or_only_default_public(sections, allow_homes=False):
    exported = [section for section in sections if section["name"].lower() not in {"global"}]
    if allow_homes:
        exported = [section for section in exported if section["name"].lower() != "homes"]
    return not exported or (len(exported) == 1 and section_is_default_public(exported[0]))


def legacy_owned_exports_are_default_public(lines, current_bounds, legacy_bounds):
    for index, line in enumerate(lines):
        if bounds_contain(current_bounds, index) or bounds_contain(legacy_bounds, index):
            continue
        marker_name = legacy_share_marker_name(line)
        if marker_name is None:
            continue
        end = marked_legacy_share_end(lines, index, marker_name)
        if end is None:
            return False, "legacy share marker is ambiguous"
        sections = samba_section_blocks(lines, index + 1, end)
        if not sections_contain_only_default_public(sections):
            return False, "legacy marked share is not the default Public share"
    if legacy_bounds is not None:
        begin, end = legacy_bounds
        sections = samba_section_blocks(lines, begin + 1, end)
        if not sections_contain_no_or_only_default_public(sections, allow_homes=True):
            return False, "legacy appliance block contains non-default shares"
    return True, "ok"


def legacy_default_public_share_evidence(text):
    lines = text.splitlines(keepends=True)
    try:
        remove_legacy_nasberry_samba_content(text)
        current_bounds = current_managed_marker_bounds(lines)
        legacy_bounds = legacy_appliance_marker_bounds(lines)
    except SambaConfigError as exc:
        return False, str(exc)
    owned_ok, owned_reason = legacy_owned_exports_are_default_public(lines, current_bounds, legacy_bounds)
    if not owned_ok:
        return False, owned_reason
    if current_bounds is not None:
        begin, end = current_bounds
        current_sections = samba_section_blocks(lines, begin + 1, end)
        if sections_contain_only_default_public(current_sections):
            return True, "current managed Public share"
        return False, "current managed section is not the default Public share"
    for index, line in enumerate(lines):
        marker_name = legacy_share_marker_name(line)
        if marker_name is None:
            continue
        if marker_name.lower() != "public":
            continue
        end = marked_legacy_share_end(lines, index, marker_name)
        if end is None:
            return False, "legacy share marker is ambiguous"
        sections = samba_section_blocks(lines, index + 1, end)
        if sections_contain_only_default_public(sections):
            return True, "legacy marked Public share"
        return False, "legacy marked Public share does not match the configured default"
    if any(line.strip() == LEGACY_APPLIANCE_MARKER for line in lines):
        return False, "standalone legacy Nasberry appliance marker is ambiguous"
    legacy_appliance_file = any(line.strip() == LEGACY_APPLIANCE_HEADER for line in lines)
    if legacy_appliance_file:
        sections = [section for section in samba_section_blocks(lines) if section_is_default_public(section)]
        if len(sections) == 1:
            return True, "legacy appliance Public share"
        if len(sections) > 1:
            return False, "legacy appliance Public share is ambiguous"
        return False, "legacy appliance Samba configuration is not the default Public share"
    if legacy_bounds is not None:
        index, end = legacy_bounds
        sections = samba_section_blocks(lines, index + 1, end)
        if sections_contain_only_default_public(sections, allow_homes=True):
            return True, "legacy appliance Public share"
        return False, "legacy appliance block is not the default Public share"
    return False, "no known legacy Nasberry default Public share was found"


def samba_config_valid():
    shares = samba_shares()
    if shares is None:
        return False, "testparm could not read the Samba configuration"
    try:
        expected = {share["name"].lower(): share for share in enabled_shares()}
    except ShareConfigError as exc:
        return False, str(exc)
    managed_ok, managed_names, managed_reason = managed_samba_share_names()
    if not managed_ok:
        return False, managed_reason
    managed = {name.lower(): name for name in managed_names}
    if set(managed) != set(expected):
        missing = [expected[key]["name"] for key in expected if key not in managed]
        stale = [managed[key] for key in managed if key not in expected]
        detail = []
        if missing:
            detail.append(f"missing managed share(s): {', '.join(missing)}")
        if stale:
            detail.append(f"stale managed share(s): {', '.join(stale)}")
        return False, "; ".join(detail)
    actual = {name.lower(): values for name, values in shares.items()}
    for key, share in expected.items():
        configured = actual.get(key)
        if not configured:
            return False, f"share [{share['name']}] was not found"
        configured_path = configured.get("path", "")
        if os.path.abspath(configured_path) != os.path.abspath(share["path"]):
            return False, f"share [{share['name']}] points to {configured_path or 'no path'}, not {share['path']}"
        read_only = configured.get("read only", "no").lower() in {"yes", "true"}
        if read_only != bool(share.get("read_only")):
            return False, f"share [{share['name']}] read-only setting does not match Nasberry config"
    return True, f"{len(expected)} enabled share(s) configured"


def start_share():
    operation_header("START SHARING", "Bringing Nasberry shared folders online")
    try:
        enabled = enabled_shares()
    except ShareConfigError as exc:
        log(f"✖ Share configuration error: {exc}")
        return False
    if not enabled:
        log("✖ No Nasberry shared folders are enabled.")
        log("Enable or create one with 'sudo nasberry shares'.")
        return False
    if not service_exists(SAMBA_SERVICE):
        log(f"✖ Samba service '{SAMBA_SERVICE}' was not found. Run 'nasberry doctor'.")
        write_state(is_mounted(), False)
        return False
    if not share_user_preflight(SHARE_USER):
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
    try:
        if not enabled_shares():
            print("\nConnection information: no Nasberry shared folders are enabled.")
            return
    except ShareConfigError as exc:
        print(f"\nConnection information unavailable: {exc}")
        return
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
    config_ok, config_detail, config_fix = configuration_check()
    results.append(check("Configuration", config_ok, config_detail, config_fix, label_width))
    results.append(check("Privileges", os.geteuid() == 0 or command_exists("sudo"), "root/sudo available" if os.geteuid() == 0 or command_exists("sudo") else "sudo unavailable", "Run Nasberry as root.", label_width))
    for command in ("mount", "umount", "lsblk", "systemctl", "smbd", "testparm", "ip", "smbpasswd", "pdbedit"):
        results.append(check(f"Required command: {command}", command_exists(command), shutil.which(command) or "missing", "Re-run install.sh.", label_width))
    section_header("STORAGE")
    if config_ok:
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
            detail = f"{len(enabled)} enabled of {len(shares)} configured"
            if not enabled:
                detail = f"{detail}; no Nasberry shares enabled"
            results.append(check("Share configuration", True, detail, label_width=label_width))
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
    else:
        results.append(check("Storage configuration", False, "not checked without valid configuration", config_fix, label_width))
    section_header("SAMBA")
    if config_ok:
        results.append(check("Samba service", service_exists(SAMBA_SERVICE), SAMBA_SERVICE, "Install Samba or choose the correct service in setup.", label_width))
        valid, reason = samba_config_valid()
        results.append(check("Samba shares", valid, reason, "Run 'sudo nasberry repair-samba' to recreate it.", label_width))
        account_ok, account_detail = samba_account_valid()
        results.append(check("Samba account", account_ok, account_detail, f"Run 'sudo smbpasswd -a {SHARE_USER}' to create or reset it." if SHARE_USER else "Run 'sudo nasberry setup'.", label_width))
    else:
        results.append(check("Samba configuration", False, "not checked without valid configuration", config_fix, label_width))
    section_header("NETWORK")
    if config_ok:
        print_connection_info()
    else:
        print("Connection information: not checked without valid configuration")
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
    if not migrate_missing_legacy_shares_file():
        return False
    try:
        shares = load_shares()
    except ShareConfigError as exc:
        log(f"✖ Share configuration error: {exc}")
        log("Inspect or repair the shares file, or run 'sudo nasberry setup' if this is a new install.")
        return False

    def save_managed_shares():
        try:
            save_shares(shares)
            return True
        except OSError as exc:
            log(f"✖ Could not save share configuration {SHARES_FILE}: {exc}")
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
            if not save_managed_shares():
                return False
            log(f"✔ Added share [{share['name']}]")
        elif choice in {"2", "3", "4"}:
            share = choose_share(shares)
            if not share:
                log("✖ Share not found")
                continue
            if choice == "2":
                share["enabled"] = not share.get("enabled", True)
                if not save_managed_shares():
                    return False
                log(f"✔ [{share['name']}] is now {'enabled' if share['enabled'] else 'disabled'}")
            elif choice == "3":
                share["read_only"] = not share.get("read_only", False)
                if not save_managed_shares():
                    return False
                log(f"✔ [{share['name']}] is now {'read-only' if share['read_only'] else 'read-write'}")
            else:
                answer = input(f"Remove [{share['name']}] from Nasberry management? Data is not deleted. [y/N]: ").strip().lower()
                if answer in {"y", "yes"}:
                    shares.remove(share)
                    if not save_managed_shares():
                        return False
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


def share_config_preflight():
    try:
        load_shares(check_filesystem=False)
        return True
    except ShareConfigError as exc:
        log(f"✖ Share configuration error: {exc}")
        return False


def migrate_missing_legacy_shares_file():
    if os.path.lexists(SHARES_FILE):
        return True
    smb_file = Path("/etc/samba/smb.conf")
    try:
        text = smb_file.read_text()
    except OSError as exc:
        log(f"✖ Could not inspect Samba configuration for legacy share migration: {exc}")
        return False
    ok, reason = legacy_default_public_share_evidence(text)
    if not ok:
        log("✖ Could not safely reconstruct the missing share configuration.")
        log(f"  Reason: {reason}")
        log(f"Restore {SHARES_FILE} from backup or run 'sudo nasberry setup'.")
        return False
    try:
        save_shares([default_share()])
    except OSError as exc:
        log(f"✖ Could not migrate legacy share configuration to {SHARES_FILE}: {exc}")
        return False
    log(f"✔ Migrated legacy Nasberry share configuration to {SHARES_FILE}")
    return True


def setup_share_config_preflight():
    if not os.path.lexists(SHARES_FILE):
        return True
    if share_config_preflight():
        return True
    log(f"Inspect or repair {SHARES_FILE}; Setup will not overwrite it automatically.")
    return False


def valid_share_user(share_user):
    return valid_share_user_value(share_user)


def share_user_exists(share_user):
    try:
        pwd.getpwnam(share_user)
        return True
    except (KeyError, TypeError):
        return False


def share_user_preflight(share_user):
    if share_user_exists(share_user):
        return True
    log(f"✖ Configured Linux user does not exist: {share_user!r}")
    log("Run 'sudo nasberry setup' to select a valid share user.")
    return False


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
        write_state(is_mounted(), False)
        return False
    result = run(sudo_cmd("systemctl", "restart", SAMBA_SERVICE))
    active = result.returncode == 0 and service_active()
    if not active:
        log(f"✖ Samba restart failed: {result.stderr.strip()}")
        write_state(is_mounted(), service_active())
        return False
    write_state(is_mounted(), active)
    return True


def restore_share_after_failed_remount(stopped_by_operation):
    if not stopped_by_operation:
        return False
    if not device_mounted_at_nas():
        log("⚠ Previous sharing service was not restored because storage is not mounted in NAS mode")
        write_state(is_mounted(), service_active())
        return False
    if not service_exists(SAMBA_SERVICE):
        log(f"⚠ Previous sharing service could not be restored because '{SAMBA_SERVICE}' was not found")
        write_state(is_mounted(), False)
        return False
    valid, reason = samba_config_valid()
    if not valid:
        log(f"⚠ Previous sharing service was not restored because Samba configuration is not safe: {reason}")
        write_state(is_mounted(), service_active())
        return False
    result = run(sudo_cmd("systemctl", "start", SAMBA_SERVICE))
    active = result.returncode == 0 and service_active()
    if active:
        log("✔ Previous sharing service restored")
    else:
        log(f"⚠ Previous sharing service could not be restored: {result.stderr.strip() or 'check systemctl status'}")
    write_state(is_mounted(), active)
    return active


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
    if not share_user_preflight(SHARE_USER):
        return False
    if not migrate_missing_legacy_shares_file():
        return False
    if not share_config_preflight():
        return False
    if not samba_config_preflight():
        return False
    if not mount_storage(repair_permissions=True, confirm_external_move=True):
        log("✖ Samba repair failed. Review the validation error above.")
        return False
    if not ensure_storage_layout() or not ensure_share_folders() or not configure_samba_share():
        log("✖ Samba repair failed. Review the validation error above.")
        if not service_active():
            log("⚠ File sharing remains offline because the repair did not complete safely.")
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
    cleaned = remove_legacy_nasberry_samba_content(text)
    lines = cleaned.splitlines(keepends=True)
    bounds = current_managed_marker_bounds(lines)
    if bounds is not None:
        begin, end = bounds
        before = "".join(lines[:begin]).rstrip()
        after = "".join(lines[end + 1:]).lstrip()
    else:
        before = cleaned.rstrip()
        after = ""
    parts = [part for part in (before, section.rstrip(), after) if part]
    return "\n\n".join(parts) + "\n"


def configure_samba_share():
    smb_file = Path("/etc/samba/smb.conf")
    if not valid_share_user(SHARE_USER):
        log("✖ Refusing to create Samba configuration with an unsafe share user")
        return False
    if not share_user_preflight(SHARE_USER):
        return False
    try:
        managed_section = appliance_samba_config()
    except ShareConfigError as exc:
        log(f"✖ Share configuration error: {exc}")
        return False
    if not samba_config_preflight():
        return False
    try:
        updated_samba_config = replace_managed_samba_section(smb_file.read_text(), managed_section)
    except SambaConfigError as exc:
        log(f"✖ Samba configuration cannot be updated safely: {exc}")
        return False
    except OSError as exc:
        log(f"✖ Could not read Samba configuration {smb_file}: {exc}")
        return False
    log("Updating the NasberryPi managed Samba section only.")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup = smb_file.with_name(f"{smb_file.name}.nasberry.{timestamp}.bak")
    try:
        shutil.copy2(smb_file, backup)
    except OSError as exc:
        log(f"✖ Could not preserve Samba configuration backup {backup}: {exc}")
        return False
    log(f"Preserved previous Samba configuration at {backup}")
    candidate = None
    try:
        descriptor, candidate_name = tempfile.mkstemp(prefix=f".{smb_file.name}.nasberry.", dir=smb_file.parent)
        candidate = Path(candidate_name)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(updated_samba_config)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(smb_file, candidate)
        syntax = run(["testparm", "-s", str(candidate)])
        if syntax.returncode != 0:
            detail = syntax.stderr.strip() or syntax.stdout.strip() or "testparm rejected the candidate configuration"
            log(f"✖ Samba config validation failed: {detail}")
            return False
        os.replace(candidate, smb_file)
    except OSError as exc:
        log(f"✖ Could not update Samba configuration {smb_file}: {exc}")
        return False
    finally:
        if candidate is not None:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
    valid, reason = samba_config_valid()
    if valid:
        log("✔ Samba configured with Nasberry managed shares")
        return True
    log(f"✖ Samba config validation failed: {reason}")
    try:
        shutil.copy2(backup, smb_file)
    except OSError as exc:
        log(f"✖ Automatic restore from backup failed: {exc}")
        log(f"Preserved backup: {backup}")
        log("Review the backup, restore a safe Samba configuration if needed, then run 'sudo nasberry repair-samba'.")
        return False
    log("✔ Restored the previous Samba configuration")
    return False


def setup(non_interactive=False, skip_pin=False, share_user_arg=None):
    operation_header("SETUP", "Configuring Nasberry appliance storage and sharing")
    if os.geteuid() != 0:
        log("✖ Setup changes system files and must run as root: sudo nasberry setup")
        return False
    if CONFIG_STATUS == "invalid" or CONFIG_RUNTIME_ERROR:
        require_valid_config("setup")
        log(f"Refusing to overwrite {CONFIG_FILE} automatically.")
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
    if not setup_share_config_preflight():
        return False
    if not ensure_default_shares_file():
        return False
    if not share_config_preflight():
        return False
    pin_hash = settings.get("pin_hash", "")
    if not skip_pin:
        if non_interactive:
            log("✖ Non-interactive setup requires --skip-pin; run interactive setup afterward to set a PIN.")
            return False
        first = getpass.getpass("Create a new NAS PIN (at least 4 characters): ").strip()
        second = getpass.getpass("Confirm NAS PIN: ").strip()
        if len(first) < 4 or first != second:
            log("✖ PINs did not match or were too short")
            return False
        pin_hash = hash_pin(first)
    previous_settings = dict(settings)
    settings["device"] = f"/dev/disk/by-uuid/{selected['uuid']}"
    settings["mount_point"] = MOUNT_POINT
    settings["share_name"] = "Public"
    settings["share_user"] = share_user
    if not skip_pin:
        settings["pin_hash"] = pin_hash
    try:
        save_config()
    except OSError as exc:
        settings.clear()
        for key, value in previous_settings.items():
            settings[key] = value
        log(f"✖ Could not save configuration {CONFIG_FILE}: {exc}")
        return False
    if not publish_config_from_disk():
        detail, fix = config_problem()
        log(f"✖ Saved configuration could not be validated: {detail}")
        if fix:
            log(f"  {fix}")
        return False
    if not share_config_preflight():
        return False
    configured = mount_storage(repair_permissions=True, confirm_external_move=False) and ensure_storage_layout() and ensure_share_folders() and configure_samba_share()
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
            try:
                has_enabled_shares = bool(enabled_shares())
            except ShareConfigError as exc:
                log(f"⚠ Connection help unavailable: {exc}")
                has_enabled_shares = False
            if has_enabled_shares:
                print_windows_credential_hint()
    else:
        log(f"✖ Setup incomplete. Core settings were saved to {CONFIG_FILE}, but Samba is not ready.")
        log("Update/reinstall Nasberry, then run 'sudo nasberry repair-samba'.")
    return configured


def terminal_size():
    return shutil.get_terminal_size(fallback=(80, 24))


def terminal_width():
    columns = max(1, terminal_size().columns)
    width = min(max(1, columns - 2), 100)
    if width > 1 and (columns - width) % 2:
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
    if not config_ready():
        detail, _fix = config_problem()
        state_text = "○ not configured" if CONFIG_STATUS == "missing" and not CONFIG_RUNTIME_ERROR else "✖ configuration error"
        return [
            f"Configuration {state_text}",
            fit_text(detail, terminal_width() - 4),
            "Storage      not checked",
            "Sharing      not checked",
            "Setup        run setup" if CONFIG_STATUS == "missing" and not CONFIG_RUNTIME_ERROR else f"Config file  {CONFIG_FILE}",
            "Diagnostics  available",
        ]
    present = device_exists()
    mount_state, mount_location = menu_mount_status()
    try:
        enabled = enabled_shares()
        enabled_count = len(enabled)
        share_detail = f"{enabled_count} enabled share(s)"
        sharing_text = "○ no shares enabled" if enabled_count == 0 else ("● sharing online" if nasberry_sharing_online(enabled) else "○ sharing offline")
    except ShareConfigError:
        share_detail = "share config error"
        sharing_text = "✖ share config error"
    return [
        f"Storage      {'● present' if present else '○ missing'}   {mount_state}",
        f"Sharing      {sharing_text}   {share_detail}",
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
    def __init__(self, input_stream=None, output_stream=None):
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self.input_tty = self.input_stream.isatty()
        self.output_tty = self.output_stream.isatty()
        self.alternate_enabled = self.input_tty and self.output_tty
        self.descriptor = self.input_stream.fileno() if self.input_tty else None
        self.original = termios.tcgetattr(self.descriptor) if self.input_tty else None
        self.navigation_active = False
        self.alternate_active = False
        self.cursor_hidden = False

    def enable_navigation_mode(self):
        if self.input_tty and not self.navigation_active:
            tty.setcbreak(self.descriptor, termios.TCSANOW)
            self.navigation_active = True

    def restore_normal_mode(self, flush_input=False, suppress_errors=False):
        if self.input_tty and self.original is not None and self.navigation_active:
            when = termios.TCSAFLUSH if flush_input else termios.TCSADRAIN
            try:
                termios.tcsetattr(self.descriptor, when, self.original)
            except Exception:
                if not suppress_errors:
                    raise
            finally:
                self.navigation_active = False

    def enter_alternate_screen(self):
        if self.alternate_enabled and not self.alternate_active:
            self.alternate_active = True
            self.output_stream.write("\033[?1049h")
            self.output_stream.flush()

    def leave_alternate_screen(self, suppress_errors=False):
        if self.alternate_enabled and self.alternate_active:
            try:
                self.output_stream.write("\033[?1049l")
                self.output_stream.flush()
            except Exception:
                if not suppress_errors:
                    raise
            finally:
                self.alternate_active = False

    def hide_cursor(self):
        if self.alternate_enabled and not self.cursor_hidden:
            self.output_stream.write("\033[?25l")
            self.cursor_hidden = True
            self.output_stream.flush()

    def show_cursor(self, suppress_errors=False):
        if self.output_tty and self.cursor_hidden:
            try:
                self.output_stream.write("\033[?25h")
                self.output_stream.flush()
            except Exception:
                if not suppress_errors:
                    raise
            finally:
                self.cursor_hidden = False

    def close(self, flush_input=True, suppress_errors=False):
        first_error = None
        for cleanup in (
            lambda: self.restore_normal_mode(flush_input=flush_input, suppress_errors=suppress_errors),
            lambda: self.show_cursor(suppress_errors=suppress_errors),
            lambda: self.leave_alternate_screen(suppress_errors=suppress_errors),
        ):
            try:
                cleanup()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None and not suppress_errors:
            raise first_error

    def __enter__(self):
        try:
            self.enter_alternate_screen()
            self.hide_cursor()
            self.enable_navigation_mode()
            return self
        except Exception:
            self.close(flush_input=True, suppress_errors=True)
            raise

    def __exit__(self, exc_type, _exc, _traceback):
        self.close(flush_input=True, suppress_errors=exc_type is not None)
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
    if not config_ready():
        detail, fix = config_problem()
        print(f"Configuration : {CONFIG_STATUS}")
        print(f"Details       : {detail}")
        if fix:
            print(f"Next step     : {fix}")
        return False
    try:
        enabled = enabled_shares()
        share_count = str(len(enabled))
        sharing_online = nasberry_sharing_online(enabled)
        sharing_text = "no shares enabled" if not enabled else ("sharing online" if sharing_online else "sharing offline")
    except ShareConfigError as exc:
        enabled = None
        sharing_online = False
        share_count = f"configuration error: {exc}"
        sharing_text = "share config error"
    print(f"Storage device : {DEVICE} ({'present' if device_exists() else 'missing'})")
    mount_point = active_mount_point()
    print(f"Mount state    : {storage_mount_state_label()}")
    print(f"File sharing   : {sharing_text}")
    print("Share mode     : Multiple shared folders")
    print(f"Enabled shares : {share_count}")
    print(f"Disk space     : {disk_usage()}")
    print(f"Mount point    : {mount_point or MOUNT_POINT}")
    print(f"Share user     : {SHARE_USER or 'not configured'}")
    if enabled and sharing_online:
        print_connection_info()

def storage_info():
    if not config_ready():
        detail, fix = config_problem()
        print(f"Configuration       : {CONFIG_STATUS}")
        print(f"Details             : {detail}")
        if fix:
            print(f"Next step           : {fix}")
        return False
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
    config_dependent_actions = {"1", "2", "3", "4", "5", "8", "9"}

    def run_selected_action(key):
        label, action = actions[key]
        terminal.restore_normal_mode(flush_input=True)
        terminal.show_cursor()
        terminal.leave_alternate_screen()
        show_action_feedback(label, action_modes.get(key, "action"))
        if key in config_dependent_actions and not require_valid_config(label):
            pass
        else:
            run_dashboard_action(action)
        status_cache.invalidate()
        pause()
        terminal.enter_alternate_screen()
        terminal.hide_cursor()
        terminal.enable_navigation_mode()

    try:
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
        terminal.close(flush_input=True, suppress_errors=sys.exc_info()[0] is not None)
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
    def guarded(operation, action):
        return require_valid_config(operation) and action()
    commands = {
        "status": lambda: (status() or True),
        "storage": lambda: (storage_info() or True),
        "online": lambda: guarded("start sharing", lambda: protected(start_share)),
        "offline": lambda: guarded("stop sharing", lambda: protected(lambda: stop_share() and unmount_storage())),
        "mount": lambda: guarded("mount storage", mount_storage),
        "unmount": lambda: guarded("unmount storage", unmount_storage),
        "lock": lambda: guarded("emergency lock", panic_lock),
        "doctor": doctor,
        "repair-samba": lambda: guarded("repair Samba", repair_samba_share),
        "shares": lambda: guarded("manage shares", manage_shares),
    }
    if args.command == "setup":
        return setup(args.non_interactive, args.skip_pin, args.share_user)
    if args.command == "safe-mode":
        if not args.yes:
            log("Refusing to disable services without --yes")
            return False
        return guarded("safe mode", enforce_boot_safety)
    if args.command in commands:
        return commands[args.command]()
    if config_ready() and SAFE_MODE_ON_START and not enforce_boot_safety():
        return False
    menu()
    return True


if __name__ == "__main__":
    try:
        raise SystemExit(0 if main() else 1)
    except KeyboardInterrupt:
        log("Interrupted; operation stopped. Run 'nasberry status' to verify the current state before retrying.")
        raise SystemExit(130)
