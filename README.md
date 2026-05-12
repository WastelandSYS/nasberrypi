<img width="2000" height="700" alt="ChatGPT_Image_May_11_2026_02_06_05_AM" src="https://github.com/user-attachments/assets/ce0fc81a-6091-48d3-8147-b1f207bc3812" />

# NasberryPi

NasberryPi is a menu-driven Raspberry Pi NAS controller for mounting a dedicated storage drive and safely starting or stopping a Samba share.

The project keeps the terminal dashboard style, but favors conservative behavior because it controls real storage.

## Current behavior

- Starts in **safe mode** by stopping supported Samba services and disabling boot auto-start.
- Mounts a configured storage device before the NAS share is allowed online.
- Stops the share before unmounting storage.
- Provides a panic lock that attempts to stop sharing and unmount storage.
- Shows a visual status dashboard with mount, share, device, disk-space, and mount-point information.

## Install

```bash
sudo ./install.sh
```

The installer copies `nasberrypi.py` into `/opt/nasberry/nasberrypi.py` and links `/usr/local/bin/nasberry` to that installed copy.

Launch with:

```bash
nasberry
```


## Copy the scripts without patch markers

If you are looking at a GitHub diff or pull request, the `+` and `-` characters are patch markers, not part of the real scripts. Use one of these safer methods instead:

### From this repository folder

Print the clean script, then copy it from your terminal output:

```bash
cat nasberrypi.py
```

Or copy it directly to another location on the same machine:

```bash
cp nasberrypi.py /path/to/your/test/nasberrypi.py
cp install.sh /path/to/your/test/install.sh
chmod +x /path/to/your/test/nasberrypi.py /path/to/your/test/install.sh
```

### From GitHub

Open the file itself, not the pull request diff, then click **Raw**. The Raw view shows the plain file with no leading `+` or `-` patch symbols.

For example, after the branch is pushed, use the Raw view for:

- `nasberrypi.py`
- `install.sh`
- `README.md`

Then use your browser's copy command or download the raw file.

## Configuration

NasberryPi can be customized with environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NASBERRY_DEVICE` | `/dev/disk/by-label/NasberryDRV` | Storage device to mount. |
| `NASBERRY_MOUNT_POINT` | `/mnt/nasberry` | Mount point used for the NAS drive. |
| `NASBERRY_GUI_MOUNT` | `/media/kali/NasberryDRV` | GUI auto-mount path to reclaim before NAS mounting. |
| `NASBERRY_STATE_FILE` | `~/.nasberry_state.log` | Runtime state file path. |
| `NASBERRY_PIN` | `1234` | PIN required for share online/offline actions. Change this before real NAS use. |
| `NASBERRY_CHECK_DELAY` | `2` | Delay, in seconds, after mount/unmount operations. |
| `NASBERRY_SAMBA_SERVICE` | `smbd` | Main Samba service controlled by the NAS menu. |
| `NASBERRY_SAMBA_SERVICES` | `smbd,nmbd,winbind` | Comma-separated services enforced during safe mode. |

Example:

```bash
export NASBERRY_PIN='change-me'
export NASBERRY_DEVICE='/dev/disk/by-uuid/YOUR-DRIVE-UUID'
nasberry
```

## Safety notes

- Do not use the default PIN on a real NAS.
- Prefer a drive UUID over a drive label for production use.
- Confirm your Samba share configuration points to the configured mount point.
- Test mount and unmount behavior with non-critical data before trusting the system with important files.

## Roadmap

Planned careful improvements:

1. First-run setup that stores a hashed PIN instead of relying on an environment variable.
2. Samba share configuration and validation with `testparm`.
3. Optional command-line subcommands for scripting while keeping the visual menu.
4. Better logging through a rotating log file or systemd journal.
5. Connected-client information from `smbstatus`.
