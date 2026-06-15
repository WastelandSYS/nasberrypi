<!-- ========================================================= -->

<!--                        HERO IMAGE                         -->

<!-- ========================================================= -->

<img width="2000" height="700" alt="NASBERRYMAINBNRM" src="https://github.com/user-attachments/assets/ce0fc81a-6091-48d3-8147-b1f207bc3812" />

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
* Automatic share configuration
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
<img width="802" height="516" alt="NasberryMainMenu" src="https://github.com/user-attachments/assets/bb58a2e4-378e-4d2a-bc20-29cbdfd69690" />
</p>

---

## Nasberry Setup

Configure storage devices, mount points, and NAS settings.

```bash
sudo nasberry setup
```

<p align="center">
<img width="850" height="650" alt="NasberrySetup" src="https://github.com/user-attachments/assets/8587829c-c981-4e9d-984b-3ad373b63503" />
</p>

---

## Nasberry Diagnostics

Run a complete health check of storage, Samba, permissions, configuration, and system requirements.

```bash
sudo nasberry doctor
```

<p align="center">
<img width="801" height="804" alt="NasberryDiagnostics" src="https://github.com/user-attachments/assets/3078d602-bb9b-4740-8ab2-7670c9475150" />
</p>

---

## Active File Sharing

Network share running and accessible from other devices.

<p align="center">
<img width="1000" height="1080" alt="NasberryWorking" src="https://github.com/user-attachments/assets/116dc657-b10d-4e2d-a4d7-c331161cac77" />
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

# UNINSTALLATION

```bash
cd nasberrypi
sudo ./uninstall.sh
```

The uninstaller removes the global `nasberry` shortcut and related application files. It does not remove your cloned repository folder.

Preview an uninstall without changing the system:

```bash
sudo ./uninstall.sh --dry-run
```

Use `--purge` to also remove Nasberry's configuration and managed Samba settings. Storage data is never deleted. Run `sudo ./uninstall.sh --help` for all options.

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

Linux:

```text
smb://hostname/Public
```

macOS:

```text
smb://hostname/Public
```

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
| Repair Samba     | Repair Samba configuration             |
| Safe Mode CLI    | Disable automatic sharing services     |
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

---

# COMPATIBILITY

Designed primarily for Linux systems.

Tested on:

* Raspberry Pi OS
* Kali Linux ARM
* Raspberry Pi 4B
* Raspberry Pi 5

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
* Setup switches Samba into Public-only appliance mode. The previous `/etc/samba/smb.conf` is backed up first, but existing custom shares may be disabled.
* Network share discovery behavior may vary by operating system.

---

# RECOVERY

Nasberry validates a candidate Samba configuration before replacing the live configuration and saves the previous configuration as `/etc/samba/smb.conf.nasberry.<timestamp>.bak`.

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

Nasberry's configuration is stored at `/etc/nasberry/config.ini`. Run `sudo nasberry doctor` for diagnostics or `sudo nasberry repair-samba` to recreate and validate the managed Public share.

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
