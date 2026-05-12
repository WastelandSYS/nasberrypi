#!/usr/bin/env python3

import os
import subprocess
import time
import getpass
from datetime import datetime

# =========================
#          CONFIG
# =========================

DEVICE = "/dev/disk/by-label/NasberryDRV"
MOUNT_POINT = "/mnt/nasberry"

STATE_FILE = os.path.expanduser("~/.nasberry_state.log")

PIN_CODE = "1234"  # change later
CHECK_DELAY = 2

# Samba service name (NAS sharing layer)
SAMBA_SERVICE = "smbd"
SAMBA_SERVICES = ["smbd", "nmbd", "winbind"]

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

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def clear():
    os.system("clear")


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

    gui_mount = "/media/kali/NasberryDRV"

    if os.path.ismount(gui_mount):

        log("Detected GUI auto-mounted drive")
        log("Reclaiming drive for NAS mode...")

        run(["sudo", "umount", gui_mount])

        time.sleep(2)

def mount_storage():

    ensure_mount_point()

    cleanup_gui_mount()

    if is_mounted():
        log("✔ Already mounted")
        return

    # CHECK DEVICE BEFORE MOUNTING
    if not device_exists():
        log("✖ Storage device not found")
        return

    log("Mounting NAS storage...")

    result = run(["sudo", "mount", DEVICE, MOUNT_POINT])

    if result.stderr:
        log(f"mount stderr: {result.stderr.strip()}")

    time.sleep(CHECK_DELAY)

    if result.returncode == 0 and is_mounted():
        log(f"✔ Mounted at {MOUNT_POINT}")
        write_state(True)
    else:
        log("✖ Mount failed")
        write_state(False)

def enforce_boot_safety():
    log("Enforcing NAS SAFE MODE boot policy...")

    for service in SAMBA_SERVICES:

        # 1. Check if service exists (real check, not just list-unit-files)
        exists = run(["systemctl", "status", service])

        if exists is None or exists.returncode != 0:
            log(f"Skipping {service} (not installed)")
            continue

        # 2. Stop service if active
        active = run(["systemctl", "is-active", service])
        if active and active.stdout.strip() == "active":
            log(f"{service} is ACTIVE → stopping")
            run(["sudo", "systemctl", "stop", service])

        # 3. Disable boot start (IMPORTANT SAFE MODE RULE)
        enabled = run(["systemctl", "is-enabled", service])
        if enabled and enabled.stdout.strip() == "enabled":
            log(f"{service} NOT auto-starting (ensuring runtime safe state)")
            run(["sudo", "systemctl", "stop", service])

    log("✔ SAFE MODE boot policy enforced")

def unmount_storage():

    if not is_mounted():
        log("✔ Already unmounted")
        return

    log("Unmounting storage...")

    result = run(["sudo", "umount", MOUNT_POINT])

    time.sleep(CHECK_DELAY)

    if result and result.returncode == 0 and not is_mounted():
        log("✔ Storage unmounted")
        write_state(False)
    else:
        log("✖ Unmount failed")


# =========================
#        SAMBA CONTROL
# =========================

def start_share():

    if not service_exists(SAMBA_SERVICE):
        log("Samba service not installed")
        log("Install may be corrupted or incomplete")
        return

    log("Starting NAS share...")

    run(["systemctl", "start", SAMBA_SERVICE])

    time.sleep(1)

    if service_active():
        log("✔ NAS SHARE ONLINE")
    else:
        log("✖ Failed to start NAS share")


def stop_share():

    log("Stopping NAS share...")

    if not service_exists(SAMBA_SERVICE):
        log("Service not found — nothing to stop")
        return

    run(["systemctl", "stop", SAMBA_SERVICE])

    time.sleep(1)

    if not service_active():
        log("✔ NAS SHARE OFFLINE")
    else:
        log("✖ Failed to stop NAS share")

def service_exists(service):
    result = run(["systemctl", "list-unit-files", service + ".service"])
    return result.returncode == 0 and service + ".service" in result.stdout

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
    return getpass.getpass("Enter NAS PIN: ").strip() == PIN_CODE


def panic_lock():
    log("!!! PANIC LOCK ACTIVATED !!!")

    stop_share()
    unmount_storage()

    log("NAS LOCKED DOWN (share stopped + storage unmounted)")


# =========================
#          STATE
# =========================

def write_state(val):
    with open(STATE_FILE, "w") as f:
        f.write(f"SHARED={1 if val else 0}\n")


def read_state():
    if not os.path.exists(STATE_FILE):
        return "UNKNOWN"
    return open(STATE_FILE).read().strip()


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
               ╔════════════════════════════════════╗
               ║            NAS STATUS              ║
               ╠════════════════════════════════════╣
               ║ Storage Mounted : {:<17}║
               ║ Share Active    : {:<17}║
               ║ Mount Point     : {:<17}║
               ╚════════════════════════════════════╝
""".format(
        str(is_mounted()),
        str(service_active()),
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

        elif choice == "2":
            if verify_pin():
                stop_share()
            else:
                log("ACCESS DENIED")

        elif choice == "3":
            mount_storage()

        elif choice == "4":
            unmount_storage()

        elif choice == "5":
            panic_lock()

        elif choice == "6":
            log("Shutting down NAS controller")
            clear()
            state["running"] = False

        else:
            log("Invalid option")


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
