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

## Install and first-time setup

```bash
sudo ./install.sh
sudo nasberry setup
```

Setup detects available drives, asks which drive to use, creates a PIN and Samba password, stores configuration in `/etc/nasberry/config.ini`, and adds a managed Samba share to `/etc/samba/smb.conf`.

> Setup creates `/etc/samba/smb.conf.nasberry.bak` before changing Samba configuration. It configures access for an existing Linux user and runs `smbpasswd` to create that user’s network password. Adjust the generated share policy if needed for your environment.

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
Windows:     \\192.168.1.25\Nasberry
macOS/Linux: smb://192.168.1.25/Nasberry
```

## Configuration

The persistent configuration file is `/etc/nasberry/config.ini` when Nasberry runs as root. Environment variables such as `NASBERRY_DEVICE`, `NASBERRY_MOUNT_POINT`, and `NASBERRY_SHARE_NAME` can temporarily override settings.

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

Nasberry setup adds a managed `[Nasberry]` section to `/etc/samba/smb.conf`, validates that section with `testparm`, and runs `smbpasswd` for the selected Linux user. To inspect the generated share manually, run:

```bash
sudo testparm -s --section-name=Nasberry
sudo testparm -s --section-name=Nasberry --parameter-name=path
```

The second command should print `/mnt/nasberry`. If it does not, run `sudo nasberry repair-samba`.

If diagnostics say `Load smb config files from Nasberry`, an older installed copy is still running. Update the local repository and reinstall it before repairing Samba:

```bash
sudo ./install.sh
nasberry --version        # Must report Nasberry 0.2.1 or newer
sudo nasberry doctor      # Shows the running version and application path
sudo nasberry repair-samba
```

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
