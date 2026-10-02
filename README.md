<!-- ========================================================= -->

<!--                        HERO IMAGE                         -->

<!-- ========================================================= -->

<img width="2172" height="724" alt="NasberryMainBnr" src="https://github.com/user-attachments/assets/9ebb7744-27e8-44f8-897c-b48323f7be36" />

# nasberrypi

Simple Raspberry Pi NAS management system with guided storage setup, Samba sharing, safe-mode controls, and one-command network storage deployment.

Turn a Raspberry Pi and a USB storage device into a personal network-attached storage server with guided setup, simplified administration, and built-in recovery tools.

---

# FEATURES

### Storage Management

* Guided storage setup wizard
* Automatic drive detection
* Mount and unmount controls
* NAS-mode and external mount awareness
* Storage validation and storage-only status reporting
* Safe storage handling and recovery

### Network Sharing

* Samba-based network file sharing
* Automatic multiple shared-folder configuration
* Cross-platform device compatibility
* Share status monitoring
* Share user management

### Safety & Recovery

* Panic Lock emergency shutdown
* Safe Mode protection
* Service validation checks
* Samba repair utilities
* Startup service management

### Administration

* Interactive terminal dashboard
* Storage and share status reporting
* Configuration management
* Mount point visibility
* Share user visibility

---

# NASBERRY STATES

| State      | Purpose                                          |
| ---------- | ------------------------------------------------ |
| Offline    | Storage safely unmounted and sharing offline     |
| Mounted    | Storage mounted in NAS mode or mounted elsewhere |
| Shared     | Storage mounted in NAS mode and sharing online    |
| Safe Mode  | Sharing services disabled until manually started |
| Panic Lock | Emergency shutdown of active shares              |

Each state is designed to provide visibility into the current status of your NAS while keeping storage management simple and predictable.

---

# SCREENSHOTS

## Main Dashboard

```bash
sudo nasberry
```

<p align="center">
<img width="702" height="484" alt="NasberrypiMainMenuV0 4 0~" src="https://github.com/user-attachments/assets/4973e340-99a8-44aa-b33d-01b0e97ecccc" />
</p>

---

## Nasberry Setup

Configure storage devices, mount points, and NAS settings.

```bash
sudo nasberry setup
```

<p align="center">
<img width="818" height="632" alt="NasberrySetup" src="https://github.com/user-attachments/assets/fcd64ed3-f7d4-4220-8e53-fa0e27dbfa92" />
</p>

---

## Nasberry Diagnostics

Run a complete health check of storage, Samba, permissions, configuration, and system requirements.

```bash
sudo nasberry doctor
```

<p align="center">
<img width="747" height="795" alt="NasberrypiDiagnosticsV0 4 0~" src="https://github.com/user-attachments/assets/7d3c05c8-6163-4e78-86c1-87c90de03eec" />
</p>

---

## Active File Sharing

Network share running and accessible from other devices.

<p align="center">
<img width="900" height="900" alt="NasberryWorking" src="https://github.com/user-attachments/assets/09a76b4c-4807-45dc-814d-bbba1bac449c" />
</p>

---

## Emergency lock

Immediately stop sharing services and secure storage access.

<p align="center">
<img width="801" height="214" alt="NasberryEmergencyLock" src="https://github.com/user-attachments/assets/658e812e-0bc9-4776-a9c4-abdb8b0ee41a" />
</p>

---

# INSTALLATION

```bash
git clone https://github.com/WastelandSYS/nasberrypi.git
cd nasberrypi
chmod +x install.sh uninstall.sh
sudo ./install.sh
```

Launch with:

```bash
sudo nasberry
```

Nasberry currently supports Debian-family systems that provide `apt-get`, including Raspberry Pi OS, Debian, Ubuntu, and Kali Linux.

---

# UPGRADE

Update the repository, reinstall the application files, repair or migrate Nasberry-owned Samba state, then run diagnostics:

```bash
cd nasberrypi
git pull
sudo ./install.sh
sudo nasberry repair-samba
sudo nasberry doctor
```

Rerunning `install.sh` replaces Nasberry application files and command links. It preserves `/etc/nasberry/config.ini`, preserves `/etc/nasberry/shares.json`, does not delete storage data, and does not replace your entire Samba configuration.

`sudo nasberry repair-samba` updates or migrates recognized Nasberry-owned Samba settings through Nasberry's backup-safe, candidate-validated writer. If `/etc/nasberry/shares.json` is genuinely missing, repair can reconstruct it only when historical Nasberry evidence proves the old installation used the default `Public` share layout. Ambiguous or custom legacy share state is refused instead of guessed; restore `/etc/nasberry/shares.json` from backup or run `sudo nasberry setup`.

---

# UNINSTALLATION

```bash
cd nasberrypi
sudo ./uninstall.sh
```

The normal uninstaller removes Nasberry application files and Nasberry-owned command links. It preserves `/etc/nasberry`, Samba configuration, storage data, installed packages, and your cloned repository folder. It does not stop Samba or unmount storage automatically.

Preview an uninstall without changing the system:

```bash
sudo ./uninstall.sh --dry-run
```

Use `--purge` to also remove `/etc/nasberry` and recognized Nasberry-owned Samba settings. Purge creates a Samba backup such as `/etc/samba/smb.conf.nasberry-uninstall.<timestamp>.bak`, validates the cleanup candidate before replacement, and preserves unrelated Samba configuration. Storage data is never deleted.

Use `--remove-mount-point` to remove the mount-point directory only when it is unmounted and empty. Run `sudo ./uninstall.sh --help` for all options.

---

# QUICK START

### 1. Connect Storage

Attach a USB SSD, HDD, or flash drive to your Raspberry Pi.

### 2. Launch NasberryPi

```bash
sudo nasberry
```

### 3. Run Storage Setup

Use the setup wizard to configure your storage device and mount point.

### 4. Configure Share Access

Create or configure your Samba share user.

### 5. Start File Sharing

Enable network sharing through the dashboard.

### 6. Connect From Another Device

Windows:

```text
\\hostname\Public
```

macOS/Linux:

```text
smb://hostname/Public
```

`Public` remains the default shared folder. Add more managed shares with:

```bash
sudo nasberry shares
```

All managed shares may be disabled. In that state Nasberry exports no managed shared folders, and `sudo nasberry online` or Start Share will refuse to report normal Nasberry sharing online until at least one share is enabled. Re-enable or create a share with `sudo nasberry shares`.

Android:

Use an SMB-compatible file manager such as:

- CX File Explorer
- Solid Explorer
- X-plore File Manager

Server:

```text
hostname
```

Port:

```text
445
```

Sign in using your Nasberry username and password.

iPhone / iPad:

1. Open the Files app
2. Tap the menu button
3. Select **Connect to Server**
4. Enter:

```text
smb://hostname
```

5. Sign in using your Nasberry username and password.

---

# USAGE

Launch the dashboard:

```bash
sudo nasberry
```

Main management functions:

| Option           | Description                            |
| ---------------- | -------------------------------------- |
| Setup Storage    | Configure NAS storage device           |
| Mount Storage    | Mount configured storage in NAS mode   |
| Unmount Storage  | Safely unmount storage                 |
| Start Share      | Enable network file sharing            |
| Stop Share       | Disable network file sharing           |
| Manage Shares    | Add, remove, enable, disable, or toggle read-only shared folders |
| Repair Samba     | Repair Samba configuration             |
| Safe Mode CLI    | Explicitly disable sharing services    |
| Panic Lock       | Immediate shutdown of sharing services |
| Status Dashboard | View NAS health and status             |

Help menu:

```bash
nasberry -h
```

Storage-only status:

```bash
sudo nasberry storage
```

This reports the configured storage device, whether it is present, its filesystem, mount state, active mount point, configured Nasberry mount point, and disk space. Mount state is reported as **mounted in NAS mode**, **mounted elsewhere**, or **safely unmounted**.

If `nasberry mount` finds the configured drive mounted elsewhere, interactive use shows the current and configured Nasberry mount points and asks before moving the drive into NAS mode. Press Enter or answer `n` to leave the existing mount untouched.

When `safe_mode_on_start=true`, automatic Safe Mode enforcement runs before entering the interactive dashboard with bare `sudo nasberry`. Explicit commands such as `status`, `doctor`, `online`, `setup`, `repair-samba`, and `shares` do not receive surprise startup Safe Mode enforcement. Use `sudo nasberry safe-mode --yes` to explicitly stop and disable configured sharing services once.

---

# COMPATIBILITY

Designed primarily for Linux systems.

Tested on:

* Raspberry Pi OS
* Kali Linux ARM
* Raspberry Pi 4B
* Raspberry Pi 5
* Raspberry Pi Zero 2w

Supported storage:

* USB SSD
* USB HDD
* USB Flash Drive

Supported clients:

* Windows
* Linux
* macOS
* Android
* iOS

Notes:

* Samba is installed automatically by the installer.
* ext4 is the recommended filesystem for Linux-based NAS deployments.
* Desktop environments may mount a configured drive outside the Nasberry mount point. Nasberry reports this as **mounted elsewhere** and asks before moving it into NAS mode.
* Setup creates a default `Public` share and stores managed shared folders in `/etc/nasberry/shares.json`.
* Nasberry manages only recognized Nasberry-owned Samba settings. Modern installations use a clearly marked NasberryPi section in `/etc/samba/smb.conf`; upgrade repair can migrate known legacy Nasberry formats while preserving unrelated Samba configuration.
* Network share discovery behavior may vary by operating system.

---

# RECOVERY

Nasberry validates a candidate Samba configuration before replacing the live configuration and saves the previous configuration as `/etc/samba/smb.conf.nasberry.<timestamp>.bak`. Purge/uninstall Samba cleanup uses backups named `/etc/samba/smb.conf.nasberry-uninstall.<timestamp>.bak`.

To inspect available backups:

```bash
sudo ls -1 /etc/samba/smb.conf.nasberry.*.bak
```

Before restoring a backup, validate it with `testparm`. Take Nasberry offline first, copy the selected backup to `/etc/samba/smb.conf`, validate the restored file, and restart Samba:

```bash
sudo nasberry offline
sudo testparm -s /etc/samba/smb.conf.nasberry.<timestamp>.bak
sudo cp /etc/samba/smb.conf.nasberry.<timestamp>.bak /etc/samba/smb.conf
sudo testparm -s /etc/samba/smb.conf
sudo systemctl restart smbd
```

Nasberry's system configuration is stored at `/etc/nasberry/config.ini`, and managed shared folders are stored at `/etc/nasberry/shares.json`. Run `sudo nasberry doctor` for diagnostics, `sudo nasberry shares` to manage folders, or `sudo nasberry repair-samba` to recreate and validate the managed Samba section.

Privileged commands should be run with `sudo` to operate on the system NAS configuration under `/etc/nasberry`. A non-root `nasberry` invocation uses the current user's `~/.config/nasberry/config.ini` and `~/.config/nasberry/shares.json`, so it may not show the root-managed system NAS state.

---

# SECURITY

The Nasberry PIN protects selected actions within Nasberry. It does not replace Linux account security, Samba passwords, SSH security, disk encryption, or physical security.

Nasberry performs privileged storage and Samba administration. Review release notes before upgrading, keep backups of important data, and test storage-related changes with a disposable drive first.

---

# WHY NASBERRYPI?

NasberryPi was built to simplify self-hosted network storage.

Instead of manually configuring Samba, mount points, permissions, and services, NasberryPi provides a guided interface that transforms a Raspberry Pi and a storage device into a reliable personal NAS in minutes.

The project focuses on:

* simple deployment
* safe storage handling
* reliable file sharing
* recovery and repair tools
* lightweight terminal administration

---

# LICENSE

NasberryPi is released under the GNU General Public License v3.0. See [`LICENSE`](LICENSE) for the full license text.

---

# AUTHOR

[WastelandSYS](https://github.com/WastelandSYS)
