<img width="2000" height="700" alt="ChatGPT_Image_May_11_2026_02_06_05_AM" src="https://github.com/user-attachments/assets/ce0fc81a-6091-48d3-8147-b1f207bc3812" />

# Nasberry

Nasberry turns a Raspberry Pi or Debian-based Linux computer and a formatted USB drive into a simple Samba network share. It includes guided drive detection, secure PIN-protected controls, diagnostics, and safe mount/unmount behavior.

## Features

- Automatically detects suitable formatted storage partitions and saves the selected drive by UUID.
- Guided first-time setup that creates a secure hashed PIN and a Samba share.
- Interactive menu for beginners and direct CLI commands for automation.
- `doctor` diagnostics with clear fixes for missing drives, commands, services, and Samba configuration.
- Displays Windows, macOS, and Linux connection addresses when sharing is online.
- Opt-in safe mode; Nasberry no longer disables Samba services every time it starts.

## Requirements

- Raspberry Pi OS, Debian, or Ubuntu with systemd.
- A formatted USB storage drive. Nasberry does **not** format drives.
- Root access for installation and setup.

## Test a local checkout before publishing

You do not need to push to GitHub or install Nasberry to test current local changes on the Raspberry Pi. From the checkout directory, run the source file directly with root privileges:

```bash
sudo ./nasberrypi.py --version
sudo ./nasberrypi.py setup
sudo ./nasberrypi.py repair-samba
sudo ./nasberrypi.py doctor
```

Running `sudo nasberry ...` uses the separately installed copy in `/opt/nasberry`, which may be older than the checkout. Once the checkout works correctly, install that exact local version with `sudo ./install.sh`.

## Install and first-time setup

```bash
sudo ./install.sh
sudo nasberry setup
```

Setup detects available drives, asks which drive to use, creates a PIN and Samba password, stores configuration in `/etc/nasberry/config.ini`, creates `/mnt/nasberry/Public`, `/mnt/nasberry/Private`, and `/mnt/nasberry/Backups`, and configures Samba appliance mode so only `[Public]` is active.

> Setup preserves the previous Samba configuration in a timestamped `/etc/samba/smb.conf.nasberry.*.bak` file, then installs a minimal appliance configuration containing only `[Public]`. The candidate configuration is checked with `testparm` before it replaces the active configuration, and Samba restarts only after final validation passes.

Setup and `repair-samba` mount the selected SSD at the configured mount point before creating the storage layout. Only `Public` is exported over Samba; `Private` and `Backups` must remain local-only and must not be exposed over Samba. **ext4 is recommended** for best reliability and Linux permissions. exFAT and NTFS may work for basic sharing, but they cannot enforce Linux folder permissions as reliably. On POSIX filesystems, Nasberry sets `Public` to mode `0775` and protects `Private` and `Backups` with mode `0700`. FAT, exFAT, and NTFS drives receive owner mount options because those filesystems do not support independent Unix permissions for individual folders.

## Terminal interface

Running `nasberry` without a command opens a responsive, keyboard-driven dashboard. Use the arrow keys (or `J`/`K`) to move, Enter to open an action, number keys as shortcuts, and `Q` to exit. The dashboard adapts to narrow terminals and keeps system status, actions, and navigation guidance visually separate.

Nasberry v0.2.6 remains the functional baseline for the interface. The tested hardware is Raspberry Pi OS with an exFAT SSD. Windows must see only `\\<pi-ip>\Public`; `Private` and `Backups` remain local-only. Interface work must not change storage layout, permissions, Samba behavior, PIN behavior, safe mode, setup/repair/doctor behavior, installer/uninstaller behavior, online/offline flow, or safe unmount behavior without explicit approval.

## Everyday use

Run the beginner-friendly menu:

```bash
nasberry
```

Or use direct commands:

```bash
nasberry status       # Show drive and sharing status
nasberry online       # Mount the configured drive and start sharing
nasberry offline      # Stop sharing and safely unmount
nasberry mount        # Mount only
nasberry unmount      # Safely unmount only
nasberry lock         # Emergency stop and unmount
nasberry doctor       # Diagnose common problems
nasberry repair-samba # Recreate and validate the configured Samba share
```

Starting or stopping normal file sharing requires the PIN created during setup. Emergency lock intentionally does not require the PIN.

## Connect from another device

When the share starts, Nasberry prints connection addresses similar to:

```text
Windows:     \\192.168.1.25\Public
macOS/Linux: smb://192.168.1.25/Public
```

## Configuration

The persistent configuration file is `/etc/nasberry/config.ini` when Nasberry runs as root. Environment variables such as `NASBERRY_DEVICE` and `NASBERRY_MOUNT_POINT` can temporarily override settings. The appliance share name is always `Public`.

Safe mode is opt-in because disabling Samba may affect unrelated shares. Run it explicitly with:

```bash
sudo nasberry safe-mode --yes
```

To apply it whenever Nasberry starts, set `safe_mode_on_start = true` in the config file.

## Troubleshooting

Start with:

```bash
sudo nasberry doctor
```

If a drive is missing or changed, reconnect it and run `sudo nasberry setup`. If a drive will not unmount, close files and applications using it before trying again.

Nasberry intentionally does not edit `/etc/fstab` automatically. If you need a boot-managed mount, use the selected drive's UUID and review the entry carefully; an incorrect fstab entry can prevent normal boot.

Nasberry setup installs a minimal Samba appliance configuration containing only an authenticated `[Public]` share. Before replacement, the previous Samba configuration is preserved in a timestamped backup. This prevents Windows from browsing `[homes]`, printer shares, the legacy mount-root share, or custom shares while Nasberry appliance mode is active. The Public share also disables symbolic-link traversal outside its folder.

```bash
sudo nasberry repair-samba
sudo testparm -s --section-name=Public --parameter-name=path
sudo testparm -s
```

The path command should print `/mnt/nasberry/Public`, and `sudo testparm -s` should list no share sections other than `[Public]`. Windows should connect directly to `\\<pi-ip>\Public` and cannot move above that share root. Windows may continue showing stale shares until it disconnects and reconnects.

If setup changes the Samba password, Windows may keep using its previous cached SMB session. Close open NAS windows, run the following in Windows Command Prompt, then reconnect to `\\<pi-ip>\Public` with the new password:

```bat
net use * /delete /y
```

`nasberry doctor` separately checks the Public-only Samba configuration, Public-folder ownership/write access, local-only `Private` and `Backups` protection, and whether the configured Samba account exists and is enabled. When storage is safely unmounted, doctor reports folder checks as not checked instead of incorrectly reporting them missing. On exFAT/FAT/NTFS, doctor explains that the local-only folders cannot have independent Unix modes.

If setup says `[Public]` points to `/mnt/nasberry/Public`, but incorrectly expects `/mnt/nasberry`, the installed `sudo nasberry` command is older than the checkout. Test the checkout directly first; no GitHub push or pull is required:

```bash
sudo ./nasberrypi.py --version
sudo ./nasberrypi.py repair-samba
sudo ./nasberrypi.py doctor
```

After the checkout works, `sudo ./install.sh` copies that exact local source into `/opt/nasberry` and updates the `nasberry` command.

The repair command recreates and validates the Samba share without asking you to select the drive or replace your Nasberry PIN again.

## Uninstall

The uninstaller is conservative by default: it removes Nasberry's application files but preserves your storage data, mount point, configuration, Samba share, and installed system packages.

Preview the default uninstall without changing anything:

```bash
sudo ./uninstall.sh --dry-run
```

Remove the Nasberry application while preserving configuration:

```bash
sudo ./uninstall.sh
# Equivalent: sudo ./install.sh --uninstall
```

Also remove `/etc/nasberry` and only the Samba section marked as managed by Nasberry:

```bash
sudo ./uninstall.sh --purge
```

Optionally remove `/mnt/nasberry` only if it is unmounted and empty:

```bash
sudo ./uninstall.sh --purge --remove-mount-point
```

The uninstaller never deletes storage data, never automatically unmounts an active drive, never stops Samba globally, validates Samba after managed-share removal, and restores the previous Samba configuration if validation fails. It preserves installed packages because Samba and system utilities may be used by other applications.

---

# LICENSE

Systempi is released under the GNU General Public License v3.0. See [`LICENSE`](LICENSE) for the full license text.

---

# AUTHOR

[WastelandSYS](https://github.com/WastelandSYS)
