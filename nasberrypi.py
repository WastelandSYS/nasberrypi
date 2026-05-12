#!/usr/bin/env python3

import getpass
import os
import shutil
import subprocess
import time
from datetime import datetime

# =========================
#          CONFIG
# =========================

DEVICE = os.environ.get("NASBERRY_DEVICE", "/dev/disk/by-label/NasberryDRV")
MOUNT_POINT = os.environ.get("NASBERRY_MOUNT_POINT", "/mnt/nasberry")
GUI_MOUNT = os.environ.get("NASBERRY_GUI_MOUNT", "/media/kali/NasberryDRV")

STATE_FILE = os.path.expanduser(
    os.environ.get("NASBERRY_STATE_FILE", "~/.nasberry_state.log")
)

PIN_CODE = os.environ.get("NASBERRY_PIN", "1234")  # change with NASBERRY_PIN
CHECK_DELAY = int(os.environ.get("NASBERRY_CHECK_DELAY", "2"))

# Samba service name (NAS sharing layer)
SAMBA_SERVICE = os.environ.get("NASBERRY_SAMBA_SERVICE", "smbd")
SAMBA_SERVICES = os.environ.get(
    "NASBERRY_SAMBA_SERVICES", "smbd,nmbd,winbind"
).split(",")

# =========================
#          STATE
# =========================

state = {
    "running": True
}

# =========================
#           UTIL
# =========================


def run(cmd):
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
    except Exception as e:
        log(f"Command failed: {e}")
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def sudo_cmd(*cmd):
    if os.geteuid() == 0:
        return list(cmd)
    return ["sudo", *cmd]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def clear():
    os.system("clear")


def pause():
    input("\n  Press Enter to continue...")


# =========================
#      NAS CORE CHECKS
# =========================


def is_mounted():
    return os.path.ismount(MOUNT_POINT)


def ensure_mount_point():
    if not os.path.exists(MOUNT_POINT):
        os.makedirs(MOUNT_POINT, exist_ok=True)


def device_exists():
    return os.path.exists(DEVICE)


def cleanup_gui_mount():
    if os.path.ismount(GUI_MOUNT):
        log("Detected GUI auto-mounted drive")
        log("Reclaiming drive for NAS mode...")

        result = run(sudo_cmd("umount", GUI_MOUNT))
        if result.returncode != 0:
            log(f"✖ Could not unmount GUI mount: {result.stderr.strip()}")
            return False

        time.sleep(CHECK_DELAY)

    return True


def mount_storage():
    ensure_mount_point()

    if not cleanup_gui_mount():
        write_state(False, service_active())
        return False

    if is_mounted():
        log("✔ Already mounted")
        write_state(True, service_active())
        return True

    # CHECK DEVICE BEFORE MOUNTING
    if not device_exists():
        log(f"✖ Storage device not found: {DEVICE}")
        write_state(False, service_active())
        return False

    log("Mounting NAS storage...")

    result = run(sudo_cmd("mount", DEVICE, MOUNT_POINT))

    if result.stderr:
        log(f"mount stderr: {result.stderr.strip()}")

    time.sleep(CHECK_DELAY)

    mounted = result.returncode == 0 and is_mounted()
    if mounted:
        log(f"✔ Mounted at {MOUNT_POINT}")
    else:
        log("✖ Mount failed")

    write_state(mounted, service_active())
    return mounted


def enforce_boot_safety():
    log("Enforcing NAS SAFE MODE boot policy...")

    for service in SAMBA_SERVICES:
        service = service.strip()
        if not service:
            continue

        if not service_exists(service):
            log(f"Skipping {service} (not installed)")
            continue

        active = run(["systemctl", "is-active", service])
        if active.stdout.strip() == "active":
            log(f"{service} is ACTIVE → stopping")
            run(sudo_cmd("systemctl", "stop", service))

        enabled = run(["systemctl", "is-enabled", service])
        if enabled.stdout.strip() == "enabled":
            log(f"{service} is enabled at boot → disabling for safe mode")
            run(sudo_cmd("systemctl", "disable", service))

    log("✔ SAFE MODE boot policy enforced")


def unmount_storage():
    if not is_mounted():
        log("✔ Already unmounted")
        write_state(False, service_active())
        return True

    if service_active():
        log("Share is still active — stopping it before unmount")
        stop_share()

    log("Unmounting storage...")

    result = run(sudo_cmd("umount", MOUNT_POINT))

    if result.stderr:
        log(f"umount stderr: {result.stderr.strip()}")

    time.sleep(CHECK_DELAY)

    unmounted = result.returncode == 0 and not is_mounted()
    if unmounted:
        log("✔ Storage unmounted")
    else:
        log("✖ Unmount failed")

    write_state(is_mounted(), service_active())
    return unmounted


# =========================
#        SAMBA CONTROL
# =========================


def start_share():
    if not service_exists(SAMBA_SERVICE):
        log("Samba service not installed")
        log("Install may be corrupted or incomplete")
        write_state(is_mounted(), False)
        return False

    if not is_mounted():
        log("Storage is not mounted — mounting before share start")
        if not mount_storage():
            log("✖ Refusing to start NAS share without mounted storage")
            write_state(False, False)
            return False

    log("Starting NAS share...")

    result = run(sudo_cmd("systemctl", "start", SAMBA_SERVICE))
    if result.stderr:
        log(f"systemctl stderr: {result.stderr.strip()}")

    time.sleep(1)

    active = service_active()
    if active:
        log("✔ NAS SHARE ONLINE")
    else:
        log("✖ Failed to start NAS share")

    write_state(is_mounted(), active)
    return active


def stop_share():
    log("Stopping NAS share...")

    if not service_exists(SAMBA_SERVICE):
        log("Service not found — nothing to stop")
        write_state(is_mounted(), False)
        return True

    result = run(sudo_cmd("systemctl", "stop", SAMBA_SERVICE))
    if result.stderr:
        log(f"systemctl stderr: {result.stderr.strip()}")

    time.sleep(1)

    stopped = not service_active()
    if stopped:
        log("✔ NAS SHARE OFFLINE")
    else:
        log("✖ Failed to stop NAS share")

    write_state(is_mounted(), service_active())
    return stopped


def service_exists(service):
    result = run(["systemctl", "list-unit-files", f"{service}.service"])
    return result.returncode == 0 and f"{service}.service" in result.stdout


def service_active():
    result = run(["systemctl", "is-active", SAMBA_SERVICE])

    return (
        result is not None and
        result.returncode == 0 and
        result.stdout.strip() == "active"
    )


# =========================
#        SECURITY
# =========================


def verify_pin():
    if PIN_CODE == "1234":
        log("⚠ Default PIN is active. Set NASBERRY_PIN before real NAS use.")
    return getpass.getpass("Enter NAS PIN: ").strip() == PIN_CODE


def panic_lock():
    log("!!! PANIC LOCK ACTIVATED !!!")

    share_stopped = stop_share()
    storage_unmounted = unmount_storage()

    if share_stopped and storage_unmounted:
        log("NAS LOCKED DOWN (share stopped + storage unmounted)")
        return True

    log("NAS LOCKDOWN INCOMPLETE — review messages above")
    return False


# =========================
#          STATE
# =========================


def write_state(mounted, shared):
    with open(STATE_FILE, "w") as f:
        f.write(f"MOUNTED={1 if mounted else 0}\n")
        f.write(f"SHARED={1 if shared else 0}\n")
        f.write(f"DEVICE={DEVICE}\n")
        f.write(f"MOUNT_POINT={MOUNT_POINT}\n")
        f.write(f"UPDATED_AT={datetime.now().isoformat(timespec='seconds')}\n")


def read_state():
    if not os.path.exists(STATE_FILE):
        return "UNKNOWN"
    with open(STATE_FILE) as f:
        return f.read().strip()


def disk_usage():
    if not is_mounted():
        return "Not mounted"

    usage = shutil.disk_usage(MOUNT_POINT)
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    return f"{free_gb:.1f}G free / {total_gb:.1f}G"


# =========================
#           UI
# =========================


def banner():
    print(r"""
  ███╗   ██╗ █████╗ ███████╗██████╗ ███████╗██████╗ ██████╗ ██╗   ██╗
  ████╗  ██║██╔══██╗██╔════╝██╔══██╗██╔════╝██╔══██╗██╔══██╗╚██╗ ██╔╝
  ██╔██╗ ██║███████║███████╗██████╔╝█████╗  ██████╔╝██████╔╝ ╚████╔╝
  ██║╚██╗██║██╔══██║╚════██║██╔══██╗██╔══╝  ██╔══██╗██╔══██╗  ╚██╔╝
  ██║ ╚████║██║  ██║███████║██████╔╝███████╗██║  ██║██║  ██║   ██║
  ╚═╝  ╚═══╝╚═╝  ╚═╝╚══════╝╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝

                 ☁ NASBERRY NETWORK STORAGE SYSTEM ☁
""")


def status():
    print(r"""
               ╔════════════════════════════════════════════╗
               ║                NAS STATUS                  ║
               ╠════════════════════════════════════════════╣
               ║ Storage Mounted : {:<25}║
               ║ Share Active    : {:<25}║
               ║ Device Present  : {:<25}║
               ║ Disk Space      : {:<25}║
               ║ Mount Point     : {:<25}║
               ╚════════════════════════════════════════════╝
""".format(
        str(is_mounted()),
        str(service_active()),
        str(device_exists()),
        disk_usage(),
        MOUNT_POINT
    ))


# =========================
#           MENU
# =========================


def menu():
    while state["running"]:
        clear()
        banner()
        status()

        print("""
                  ╔══════════════════════════════╗
                  ║            MENU              ║
                  ╠══════════════════════════════╣
                  ║ 1) Bring NAS Online          ║
                  ║ 2) Take NAS Offline          ║
                  ║ 3) Mount Storage             ║
                  ║ 4) Unmount Storage           ║
                  ║ 5) Panic Lock                ║
                  ║ 6) Exit                      ║
                  ╚══════════════════════════════╝
""")

        choice = input("  NAS > ").strip()

        if choice == "1":
            if verify_pin():
                start_share()
            else:
                log("ACCESS DENIED")
            pause()

        elif choice == "2":
            if verify_pin():
                stop_share()
            else:
                log("ACCESS DENIED")
            pause()

        elif choice == "3":
            mount_storage()
            pause()

        elif choice == "4":
            unmount_storage()
            pause()

        elif choice == "5":
            panic_lock()
            pause()

        elif choice == "6":
            log("Shutting down NAS controller")
            clear()
            state["running"] = False

        else:
            log("Invalid option")
            pause()


# =========================
#           ENTRY
# =========================

if __name__ == "__main__":
    try:
        clear()
        enforce_boot_safety()
        menu()

    except KeyboardInterrupt:
        log("CTRL+C detected — shutting down cleanly")
        state["running"] = False
        clear()
