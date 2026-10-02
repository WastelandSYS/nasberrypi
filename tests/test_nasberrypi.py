import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("nasberrypi", Path(__file__).parents[1] / "nasberrypi.py")
nasberrypi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nasberrypi)


class NasberryTests(unittest.TestCase):
    class FakeTTY:
        def __init__(self, data="", tty=True):
            self.data = data
            self.tty = tty

        def isatty(self):
            return self.tty

        def fileno(self):
            return 7

        def read(self, size=1):
            value = self.data[:size]
            self.data = self.data[size:]
            return value

        def readline(self, _size=-1):
            if "\n" in self.data:
                index = self.data.index("\n") + 1
                value = self.data[:index]
                self.data = self.data[index:]
                return value
            value = self.data
            self.data = ""
            return value

    class FakeOutput:
        def __init__(self, tty=True, events=None):
            self.tty = tty
            self.writes = []
            self.flushed = False
            self.events = events

        def isatty(self):
            return self.tty

        def write(self, value):
            self.writes.append(value)
            if self.events is not None:
                if value == "\033[?1049h":
                    self.events.append("enter_alt")
                elif value == "\033[?1049l":
                    self.events.append("leave_alt")

        def flush(self):
            self.flushed = True

    def setUp(self):
        self._config_state = (
            nasberrypi.CONFIG_STATUS,
            nasberrypi.CONFIG_ERROR,
            nasberrypi.CONFIG_RUNTIME_ERROR,
        )
        nasberrypi.CONFIG_STATUS = "valid"
        nasberrypi.CONFIG_ERROR = None
        nasberrypi.CONFIG_RUNTIME_ERROR = None

    def tearDown(self):
        nasberrypi.CONFIG_STATUS, nasberrypi.CONFIG_ERROR, nasberrypi.CONFIG_RUNTIME_ERROR = self._config_state

    def dashboard_actions(self):
        return {
            "1": ("Start sharing files", None),
            "2": ("Stop sharing files", None),
            "3": ("Connect storage drive", None),
            "4": ("Safely eject storage drive", None),
            "5": ("Emergency lock", None),
            "6": ("Diagnostics", None),
            "7": ("Setup / change drive", None),
            "8": ("Repair Samba share", None),
            "9": ("Manage shared folders", None),
        }

    def dashboard_status_lines(self):
        return [
            "Storage      ● present   ● mounted in NAS mode",
            "Sharing      ● sharing online   1 enabled share(s)",
            "Mount point  /mnt/nasberry",
            "Share user   nasberry",
            "Share mode   Multiple shared folders",
            "Space        10.0G free / 20.0G",
        ]

    def test_load_config_ignores_malformed_file_and_preserves_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text("[broken")
            loaded = nasberrypi.load_config(config_file)
        self.assertEqual(loaded["nasberry"]["mount_point"], "/mnt/nasberry")

    def test_load_config_reports_missing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            loaded, status, error = nasberrypi.load_config_with_status(config_file)
        self.assertEqual(status, "missing")
        self.assertIn("not configured", error)
        self.assertEqual(loaded["nasberry"]["mount_point"], "/mnt/nasberry")

    def test_load_config_accepts_complete_and_partial_legacy_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text(
                "[nasberry]\n"
                "device = /dev/custom\n"
                "mount_point = /srv/nasberry\n"
                "share_user = sparkles\n"
                "unknown_future_key = keep-me\n"
            )
            loaded, status, error = nasberrypi.load_config_with_status(config_file)
        self.assertEqual(status, "valid")
        self.assertIsNone(error)
        self.assertEqual(loaded["nasberry"]["device"], "/dev/custom")
        self.assertEqual(loaded["nasberry"]["check_delay"], "2")
        self.assertEqual(loaded["nasberry"]["unknown_future_key"], "keep-me")

    def test_load_config_rejects_broken_ini_transactionally(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            for content in (
                "[nasberry]\ndevice = /dev/custom\n[broken\n",
                "[nasberry]\ndevice = /dev/custom\ndevice = /dev/other\n",
                "[nasberry]\ndevice = /dev/custom\n[nasberry]\nmount_point = /srv/nas\n",
            ):
                config_file.write_text(content)
                loaded, status, error = nasberrypi.load_config_with_status(config_file)
                self.assertEqual(status, "invalid")
                self.assertIn("invalid configuration", error)
                self.assertEqual(loaded["nasberry"]["device"], nasberrypi.DEFAULTS["device"])

    def test_load_config_rejects_missing_section_and_unreadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text("[other]\ndevice = /dev/custom\n")
            _loaded, status, error = nasberrypi.load_config_with_status(config_file)
            self.assertEqual(status, "invalid")
            self.assertIn("[nasberry]", error)
        unreadable = mock.Mock()
        unreadable.exists.return_value = True
        unreadable.open.side_effect = OSError("denied")
        _loaded, status, error = nasberrypi.load_config_with_status(unreadable)
        self.assertEqual(status, "invalid")
        self.assertIn("denied", error)

    def test_load_config_validates_critical_setting_values(self):
        cases = (
            ("check_delay = banana", "check_delay"),
            ("check_delay = -1", "check_delay"),
            ("safe_mode_on_start = maybe", "safe_mode_on_start"),
            ("mount_point = ", "mount_point"),
            ("mount_point = relative/path", "mount_point"),
            ("mount_point = /", "mount_point"),
            ("device = ", "device"),
            ("share_user = bad user", "share_user"),
            ("samba_service = smbd;rm", "samba_service"),
            ("samba_services = ", "samba_services"),
            ("samba_services = smbd,bad/service", "samba_services"),
            ("state_file = bad\u0001file", "state_file"),
        )
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            for line, detail in cases:
                config_file.write_text(f"[nasberry]\n{line}\n")
                _loaded, status, error = nasberrypi.load_config_with_status(config_file)
                self.assertEqual(status, "invalid")
                self.assertIn(detail, error)

    def test_load_config_accepts_supported_delay_and_safe_mode_values(self):
        for value in ("true", "false", "yes", "no", "on", "off", "1", "0", "TRUE", "False"):
            with tempfile.TemporaryDirectory() as directory:
                config_file = Path(directory) / "config.ini"
                config_file.write_text(f"[nasberry]\ncheck_delay = 0\nsafe_mode_on_start = {value}\n")
                _loaded, status, error = nasberrypi.load_config_with_status(config_file)
                self.assertEqual(status, "valid")
                self.assertIsNone(error)

    def test_invalid_environment_override_blocks_config_dependent_operations(self):
        with mock.patch.dict(os.environ, {"NASBERRY_CHECK_DELAY": "banana"}):
            nasberrypi.refresh_settings()
            self.assertIsNotNone(nasberrypi.CONFIG_RUNTIME_ERROR)
            self.assertFalse(nasberrypi.config_ready())
        nasberrypi.refresh_settings()
        self.assertIsNone(nasberrypi.CONFIG_RUNTIME_ERROR)

    def test_save_config_is_private_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file):
                nasberrypi.save_config()
            self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob(".config.ini.*")), [])

    def test_save_config_preserves_primary_failure_when_cleanup_fails(self):
        primary = OSError("primary config failure")
        cleanup = PermissionError("cleanup failure")
        original_unlink = Path.unlink
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text("original config")

            def unlink(path, *args, **kwargs):
                if path.parent == config_file.parent and path.name.startswith(".config.ini."):
                    raise cleanup
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file), \
                 mock.patch.object(nasberrypi.os, "fsync", side_effect=primary), \
                 mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                with self.assertRaises(OSError) as raised:
                    nasberrypi.save_config()
            self.assertIs(raised.exception, primary)
            self.assertEqual(config_file.read_text(), "original config")

    def test_share_user_rejects_samba_configuration_injection(self):
        self.assertTrue(nasberrypi.valid_share_user("nasuser"))
        self.assertFalse(nasberrypi.valid_share_user("user\nadmin users = root"))
        self.assertFalse(nasberrypi.valid_share_user("user,root"))

    def test_pin_hash_round_trip(self):
        stored = nasberrypi.hash_pin("correct horse")
        self.assertTrue(nasberrypi.verify_pin_value("correct horse", stored))
        self.assertFalse(nasberrypi.verify_pin_value("wrong", stored))

    def test_candidate_score_prefers_removable_labeled_drive(self):
        removable = {"removable": True, "label": "Nasberry Storage", "uuid": "123"}
        internal = {"removable": False, "label": "Data", "uuid": "456"}
        self.assertGreater(nasberrypi.candidate_score(removable), nasberrypi.candidate_score(internal))

    @mock.patch.object(nasberrypi, "lsblk_devices")
    def test_device_mount_points_resolves_device_path(self, lsblk_devices):
        lsblk_devices.return_value = [{"path": "/dev/sdb1", "mountpoints": ["/media/user/disk"]}]
        with mock.patch.object(nasberrypi.os.path, "realpath", side_effect=lambda value: value):
            self.assertEqual(nasberrypi.device_mount_points("/dev/sdb1"), ["/media/user/disk"])

    def test_appliance_config_exports_default_public_with_safe_options(self):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "load_shares", return_value=[nasberrypi.default_share()]):
            config = nasberrypi.appliance_samba_config()
        self.assertIn("[Public]", config)
        self.assertIn(f"path = {nasberrypi.MOUNT_POINT}/Public", config)
        self.assertIn("valid users = kali", config)
        self.assertIn("force user = kali", config)
        self.assertNotIn("[homes]", config)
        self.assertNotIn("[Nasberry]", config)
        self.assertNotIn("[Private]", config)
        self.assertNotIn("[Backups]", config)

    def test_appliance_config_supports_empty_managed_section(self):
        with mock.patch.object(nasberrypi, "load_shares", return_value=[]):
            config = nasberrypi.appliance_samba_config()
        self.assertIn(nasberrypi.NASBERRY_SAMBA_BEGIN, config)
        self.assertIn(nasberrypi.NASBERRY_SAMBA_END, config)
        self.assertNotIn("[Public]", config)

    def test_managed_samba_share_names_parses_only_managed_block(self):
        text = f"""[OtherShare]
   path = /srv/other

{nasberrypi.NASBERRY_SAMBA_BEGIN}
# Managed by NasberryPi. Edit with 'sudo nasberry shares'.
[Public]
   path = /mnt/nasberry/Public
[Media]
   path = /mnt/nasberry/Media
{nasberrypi.NASBERRY_SAMBA_END}
"""
        ok, names, reason = nasberrypi.managed_samba_share_names(text)
        self.assertTrue(ok, reason)
        self.assertEqual(names, ["Public", "Media"])

    def test_managed_samba_share_names_accepts_empty_block_and_ignores_unrelated_shares(self):
        text = f"""[OtherShare]
   path = /srv/other

{nasberrypi.NASBERRY_SAMBA_BEGIN}
# Managed by NasberryPi. Edit with 'sudo nasberry shares'.
{nasberrypi.NASBERRY_SAMBA_END}
"""
        ok, names, reason = nasberrypi.managed_samba_share_names(text)
        self.assertTrue(ok, reason)
        self.assertEqual(names, [])

    def test_managed_samba_share_names_rejects_missing_malformed_and_duplicate_markers(self):
        cases = (
            "[Public]\npath = /mnt/nasberry/Public\n",
            f"{nasberrypi.NASBERRY_SAMBA_BEGIN}\n[Public]\n",
            f"[Public]\n{nasberrypi.NASBERRY_SAMBA_END}\n",
            f"{nasberrypi.NASBERRY_SAMBA_BEGIN}\n{nasberrypi.NASBERRY_SAMBA_BEGIN}\n{nasberrypi.NASBERRY_SAMBA_END}\n",
            f"{nasberrypi.NASBERRY_SAMBA_BEGIN}\n{nasberrypi.NASBERRY_SAMBA_END}\n{nasberrypi.NASBERRY_SAMBA_END}\n",
            f"{nasberrypi.NASBERRY_SAMBA_END}\n{nasberrypi.NASBERRY_SAMBA_BEGIN}\n",
        )
        for text in cases:
            ok, _names, reason = nasberrypi.managed_samba_share_names(text)
            self.assertFalse(ok, text)
            self.assertNotEqual(reason, "ok")

    def test_managed_samba_share_names_rejects_duplicate_share_headers_case_insensitively(self):
        text = f"""{nasberrypi.NASBERRY_SAMBA_BEGIN}
[Public]
[public]
{nasberrypi.NASBERRY_SAMBA_END}
"""
        ok, _names, reason = nasberrypi.managed_samba_share_names(text)
        self.assertFalse(ok)
        self.assertIn("duplicate", reason)

    @mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False}])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, ["Public"], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_accepts_public_only(self, samba_shares, _managed, _load_shares):
        samba_shares.return_value = {"Public": {"path": nasberrypi.public_share_path(), "available": "yes"}}
        self.assertEqual(nasberrypi.samba_config_valid(), (True, "1 enabled share(s) configured"))

    def test_share_validation_rejects_duplicates_traversal_and_external_paths(self):
        self.assertEqual(nasberrypi.validate_share({"name": "", "path": "Public"})[0], False)
        self.assertEqual(
            nasberrypi.validate_share({"name": "Public", "path": "Media"}, [{"name": "public", "path": "/mnt/nasberry/Public"}])[0],
            False,
        )
        self.assertEqual(nasberrypi.validate_share({"name": "Bad", "path": "../Bad"})[0], False)
        self.assertEqual(nasberrypi.validate_share({"name": "Bad", "path": "/srv/Bad"})[0], False)

    def test_share_validation_rejects_samba_path_injection(self):
        valid, reason = nasberrypi.validate_share({"name": "Bad", "path": "Bad\nadmin users = root"})
        self.assertFalse(valid)
        self.assertIn("unsupported characters", reason)

    def test_share_validation_rejects_symlink_parent_escape(self):
        with tempfile.TemporaryDirectory() as mount, tempfile.TemporaryDirectory() as outside:
            Path(mount, "link").symlink_to(outside, target_is_directory=True)
            share = {"name": "Escaped", "path": str(Path(mount) / "link" / "Escaped")}
            with mock.patch.object(nasberrypi, "MOUNT_POINT", mount):
                valid, reason = nasberrypi.validate_share(share)
            self.assertFalse(valid)
            self.assertIn("under", reason)

    @mock.patch.object(nasberrypi, "load_shares")
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, ["Public", "Media"], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_accepts_multiple_enabled_shares(self, samba_shares, _managed, load_shares):
        load_shares.return_value = [
            {"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False},
            {"name": "Media", "path": "/mnt/nasberry/Media", "enabled": True, "read_only": True},
            {"name": "Archive", "path": "/mnt/nasberry/Archive", "enabled": False, "read_only": False},
        ]
        samba_shares.return_value = {
            "Public": {"path": "/mnt/nasberry/Public", "read only": "no"},
            "Media": {"path": "/mnt/nasberry/Media", "read only": "yes"},
        }
        self.assertEqual(nasberrypi.samba_config_valid(), (True, "2 enabled share(s) configured"))

    @mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False}])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, ["Public"], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_rejects_mount_root(self, samba_shares, _managed, _load_shares):
        samba_shares.return_value = {"Public": {"path": nasberrypi.MOUNT_POINT, "available": "yes"}}
        valid, reason = nasberrypi.samba_config_valid()
        self.assertFalse(valid)
        self.assertIn(nasberrypi.public_share_path(), reason)

    @mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False}])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, ["Public"], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_ignores_unrelated_active_share(self, samba_shares, _managed, _load_shares):
        samba_shares.return_value = {
            "homes": {"available": "yes"},
            "Public": {"path": nasberrypi.public_share_path(), "available": "yes"},
        }
        valid, reason = nasberrypi.samba_config_valid()
        self.assertTrue(valid)
        self.assertIn("enabled share", reason)

    @mock.patch.object(nasberrypi, "load_shares", return_value=[])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, [], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares", return_value={"OtherShare": {"path": "/srv/other"}})
    def test_samba_config_accepts_zero_enabled_empty_managed_section(self, _samba_shares, _managed, _load_shares):
        self.assertEqual(nasberrypi.samba_config_valid(), (True, "0 enabled share(s) configured"))

    @mock.patch.object(nasberrypi, "load_shares", return_value=[])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, ["Public"], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares", return_value={"Public": {"path": "/mnt/nasberry/Public"}})
    def test_samba_config_rejects_stale_managed_share_when_zero_enabled(self, _samba_shares, _managed, _load_shares):
        valid, reason = nasberrypi.samba_config_valid()
        self.assertFalse(valid)
        self.assertIn("stale managed share", reason)

    @mock.patch.object(nasberrypi, "load_shares", return_value=[
        {"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False},
        {"name": "Media", "path": "/mnt/nasberry/Media", "enabled": False, "read_only": False},
    ])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, ["Public"], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares", return_value={"Public": {"path": "/mnt/nasberry/Public", "read only": "no"}})
    def test_samba_config_accepts_disabled_share_absent_from_managed_section(self, _samba_shares, _managed, _load_shares):
        self.assertEqual(nasberrypi.samba_config_valid(), (True, "1 enabled share(s) configured"))

    @mock.patch.object(nasberrypi, "load_shares", return_value=[
        {"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False},
        {"name": "Media", "path": "/mnt/nasberry/Media", "enabled": False, "read_only": False},
    ])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(True, ["Public", "Media"], "ok"))
    @mock.patch.object(nasberrypi, "samba_shares", return_value={
        "Public": {"path": "/mnt/nasberry/Public", "read only": "no"},
        "Media": {"path": "/mnt/nasberry/Media", "read only": "no"},
    })
    def test_samba_config_rejects_disabled_stale_managed_share(self, _samba_shares, _managed, _load_shares):
        valid, reason = nasberrypi.samba_config_valid()
        self.assertFalse(valid)
        self.assertIn("stale managed share", reason)

    @mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False}])
    @mock.patch.object(nasberrypi, "managed_samba_share_names", return_value=(False, [], "Nasberry managed Samba section was not found"))
    @mock.patch.object(nasberrypi, "samba_shares", return_value={"Public": {"path": "/mnt/nasberry/Public"}})
    def test_samba_config_rejects_missing_managed_markers(self, _samba_shares, _managed, _load_shares):
        valid, reason = nasberrypi.samba_config_valid()
        self.assertFalse(valid)
        self.assertIn("managed Samba section", reason)

    @mock.patch.object(nasberrypi, "load_shares", side_effect=nasberrypi.ShareConfigError("invalid shares"))
    @mock.patch.object(nasberrypi, "samba_shares", return_value={})
    def test_samba_config_valid_reports_share_config_error(self, _samba_shares, _load_shares):
        valid, reason = nasberrypi.samba_config_valid()
        self.assertFalse(valid)
        self.assertEqual(reason, "invalid shares")

    @mock.patch.object(nasberrypi.pwd, "getpwnam")
    @mock.patch.object(nasberrypi, "lsblk_devices")
    def test_exfat_mount_options_make_share_user_owner(self, lsblk_devices, getpwnam):
        lsblk_devices.return_value = [{"path": "/dev/sda1", "fstype": "exfat"}]
        getpwnam.return_value = mock.Mock(pw_uid=1000, pw_gid=1000)
        with mock.patch.object(nasberrypi, "DEVICE", "/dev/sda1"), mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            self.assertEqual(nasberrypi.storage_mount_options(), ["-o", "uid=1000,gid=1000,umask=0002"])

    @mock.patch.object(nasberrypi.pwd, "getpwnam")
    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=False)
    def test_unix_permission_filesystem_mount_options_do_not_require_share_user(self, _mount_perms, getpwnam):
        with mock.patch.object(nasberrypi, "SHARE_USER", ""):
            self.assertEqual(nasberrypi.storage_mount_options(), [])
        getpwnam.assert_not_called()

    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=True)
    def test_mount_permission_filesystem_options_fail_without_share_user(self, _mount_perms):
        with mock.patch.object(nasberrypi, "SHARE_USER", ""):
            self.assertIsNone(nasberrypi.storage_mount_options())

    @mock.patch.object(nasberrypi.pwd, "getpwnam", side_effect=KeyError)
    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=True)
    def test_mount_permission_filesystem_options_fail_for_missing_share_user(self, _mount_perms, _getpwnam):
        with mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"):
            self.assertIsNone(nasberrypi.storage_mount_options())

    @mock.patch.object(nasberrypi.pwd, "getpwnam", side_effect=TypeError)
    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=True)
    def test_mount_permission_filesystem_options_fail_for_invalid_share_user(self, _mount_perms, _getpwnam):
        with mock.patch.object(nasberrypi, "SHARE_USER", object()):
            self.assertIsNone(nasberrypi.storage_mount_options())

    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False}])
    @mock.patch.object(nasberrypi, "samba_config_valid", return_value=(True, "/mnt/nasberry/Public"))
    @mock.patch.object(nasberrypi, "run")
    def test_configure_samba_validates_candidate_before_replacing_live_config(self, run, _valid, _load_shares, _preflight):
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text("original config")
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), \
                 mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
                 mock.patch.object(nasberrypi, "share_user_preflight", return_value=True):
                self.assertTrue(nasberrypi.configure_samba_share())
            self.assertIn("[Public]", smb_file.read_text())
            self.assertIn("original config", smb_file.read_text())
            self.assertTrue(list(Path(directory).glob("smb.conf.nasberry.*.bak")))
        self.assertEqual(run.call_args.args[0][:2], ["testparm", "-s"])

    def test_refresh_settings_uses_configured_share_name(self):
        original = nasberrypi.settings.get("share_name")
        try:
            nasberrypi.settings["share_name"] = "Media"
            nasberrypi.refresh_settings()
            self.assertEqual(nasberrypi.SHARE_NAME, "Media")
        finally:
            nasberrypi.settings["share_name"] = original
            nasberrypi.refresh_settings()

    def test_load_shares_parses_string_booleans_conservatively(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text(
                '{"shares": [{"name": "Media", "path": "Media", "enabled": "false", "read_only": "false"}]}'
            )
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                shares = nasberrypi.load_shares()
        self.assertEqual(shares[0]["enabled"], False)
        self.assertEqual(shares[0]["read_only"], False)

    def test_load_shares_rejects_invalid_field_types_and_booleans(self):
        cases = (
            ("enabled", "banana", "share 1: enabled must be a boolean"),
            ("read_only", "banana", "share 1: read_only must be a boolean"),
            ("enabled", 2, "share 1: enabled must be a boolean"),
            ("read_only", {}, "share 1: read_only must be a boolean"),
            ("name", 123, "share 1: name must be a string"),
            ("name", [], "share 1: name must be a string"),
            ("path", 123, "share 1: path must be a string"),
            ("path", {}, "share 1: path must be a string"),
        )
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                for field, value, pattern in cases:
                    item = {"name": "Media", "path": "Media"}
                    item[field] = value
                    shares_file.write_text(json.dumps({"shares": [item]}))
                    with self.assertRaisesRegex(nasberrypi.ShareConfigError, pattern):
                        nasberrypi.load_shares()

    def test_load_shares_accepts_supported_boolean_values(self):
        cases = (
            (True, False, True, False),
            (False, True, False, True),
            ("true", "false", True, False),
            ("yes", "no", True, False),
            ("on", "off", True, False),
            ("1", "0", True, False),
            (1, 0, True, False),
            (None, None, True, False),
        )
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                for enabled, read_only, expected_enabled, expected_read_only in cases:
                    shares_file.write_text(
                        json.dumps(
                            {
                                "shares": [
                                    {
                                        "name": "Media",
                                        "path": "Media",
                                        "enabled": enabled,
                                        "read_only": read_only,
                                    }
                                ]
                            }
                        )
                    )
                    share = nasberrypi.load_shares()[0]
                    self.assertEqual(share["enabled"], expected_enabled)
                    self.assertEqual(share["read_only"], expected_read_only)
                shares_file.write_text(json.dumps({"shares": [{"name": "Media", "path": "Media"}]}))
                share = nasberrypi.load_shares()[0]
                self.assertTrue(share["enabled"])
                self.assertFalse(share["read_only"])

    def test_load_shares_missing_file_raises_share_config_error(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                with self.assertRaisesRegex(nasberrypi.ShareConfigError, "missing"):
                    nasberrypi.load_shares()

    def test_load_shares_accepts_modern_and_legacy_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                shares_file.write_text('{"shares": [{"name": "Media", "path": "Media"}]}')
                self.assertEqual(nasberrypi.load_shares()[0]["name"], "Media")
                shares_file.write_text('[{"name": "Archive", "path": "Archive"}]')
                self.assertEqual(nasberrypi.load_shares()[0]["name"], "Archive")

    def test_load_shares_rejects_malformed_or_unreadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text("{broken")
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                with self.assertRaisesRegex(nasberrypi.ShareConfigError, "invalid JSON"):
                    nasberrypi.load_shares()
            unreadable = mock.Mock()
            unreadable.read_text.side_effect = OSError("denied")
            with mock.patch.object(nasberrypi, "SHARES_FILE", unreadable):
                with self.assertRaisesRegex(nasberrypi.ShareConfigError, "could not read"):
                    nasberrypi.load_shares()

    def test_load_shares_rejects_unexpected_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                for content, pattern in (
                    ('"bad"', "must be an object"),
                    ("{}", "'shares' list"),
                    ('{"shares": "bad"}', "list of shares"),
                    ('{"shares": [1]}', "share 1 is not an object"),
                ):
                    shares_file.write_text(content)
                    with self.assertRaisesRegex(nasberrypi.ShareConfigError, pattern):
                        nasberrypi.load_shares()

    def test_load_shares_rejects_invalid_duplicate_or_mixed_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                for content, pattern in (
                    ('{"shares": [{"name": "", "path": "Public"}]}', "share 1"),
                    ('{"shares": [{"name": "Bad", "path": "/srv/Bad"}]}', "under"),
                    ('{"shares": [{"name": "Public", "path": "Public"}, {"name": "public", "path": "Other"}]}', "duplicate"),
                    ('{"shares": [{"name": "Public", "path": "Public"}, {"name": "", "path": "Other"}]}', "share 2"),
                ):
                    shares_file.write_text(content)
                    with self.assertRaisesRegex(nasberrypi.ShareConfigError, pattern):
                        nasberrypi.load_shares()

    def test_share_config_preflight_ignores_underlying_mount_point_symlinks(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            mount = Path(directory) / "nasberry"
            mount.mkdir()
            (mount / "Media").symlink_to(outside, target_is_directory=True)
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text('{"shares": [{"name": "Media", "path": "Media"}]}')
            with mock.patch.object(nasberrypi, "MOUNT_POINT", str(mount)), \
                 mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                self.assertTrue(nasberrypi.share_config_preflight())
                with self.assertRaisesRegex(nasberrypi.ShareConfigError, "under"):
                    nasberrypi.load_shares()

    def test_structural_share_validation_uses_lexical_path_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory) / "nasberry"
            sibling = Path(directory) / "nasberry2"
            mount.mkdir()
            sibling.mkdir()
            with mock.patch.object(nasberrypi, "MOUNT_POINT", str(mount)):
                accepted = ("Media", str(mount / "Media"))
                rejected = ("../Media", "/srv/Media", str(sibling / "Media"))
                for value in accepted:
                    share = {"name": "Media", "path": value}
                    ok, reason = nasberrypi.validate_share(share, check_filesystem=False)
                    self.assertTrue(ok, reason)
                    self.assertEqual(share["path"], os.path.abspath(os.path.normpath(share["path"])))
                for value in rejected:
                    ok, reason = nasberrypi.validate_share({"name": "Media", "path": value}, check_filesystem=False)
                    self.assertFalse(ok, value)
                    self.assertIn("under" if value != "../Media" else "..", reason)

    def test_load_shares_preserves_empty_and_all_disabled_configurations(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                shares_file.write_text('{"shares": []}')
                self.assertEqual(nasberrypi.load_shares(), [])
                shares_file.write_text('{"shares": [{"name": "Media", "path": "Media", "enabled": false}]}')
                shares = nasberrypi.load_shares()
        self.assertEqual(shares[0]["name"], "Media")
        self.assertFalse(shares[0]["enabled"])
        with mock.patch.object(nasberrypi, "load_shares", return_value=shares):
            self.assertEqual(nasberrypi.enabled_shares(), [])

    def test_save_shares_writes_json_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            share = {"name": "Media", "path": "/mnt/nasberry/Media", "enabled": False, "read_only": True}
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                nasberrypi.save_shares([share])
            self.assertEqual(json.loads(shares_file.read_text()), {"shares": [share]})
            self.assertEqual(shares_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob(".shares.json.*")), [])

    def test_save_shares_preserves_primary_failure_when_cleanup_fails(self):
        primary = OSError("primary save failure")
        cleanup = PermissionError("cleanup failure")
        original_unlink = Path.unlink
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            original = '{"shares":[{"name":"Keep"}]}\n'
            shares_file.write_text(original)

            def unlink(path, *args, **kwargs):
                if path.parent == shares_file.parent and path.name.startswith(".shares.json."):
                    raise cleanup
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi.os, "fsync", side_effect=primary), \
                 mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                with self.assertRaises(OSError) as raised:
                    nasberrypi.save_shares([])
            self.assertIs(raised.exception, primary)
            self.assertEqual(shares_file.read_text(), original)

    def test_save_shares_removes_temp_file_after_primary_failure(self):
        primary = OSError("primary save failure")
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            original = '{"shares":[{"name":"Keep"}]}\n'
            shares_file.write_text(original)
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi.os, "fsync", side_effect=primary):
                with self.assertRaises(OSError) as raised:
                    nasberrypi.save_shares([])
            self.assertIs(raised.exception, primary)
            self.assertEqual(shares_file.read_text(), original)
            self.assertEqual(list(Path(directory).glob(".shares.json.*")), [])

    def test_ensure_default_shares_file_bootstraps_only_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            dangling = Path(directory) / "dangling-shares.json"
            dangling.symlink_to(Path(directory) / "missing-target.json")
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                self.assertTrue(nasberrypi.ensure_default_shares_file())
                self.assertIn("Public", shares_file.read_text())
                shares_file.write_text("{broken")
                self.assertTrue(nasberrypi.ensure_default_shares_file())
                self.assertEqual(shares_file.read_text(), "{broken")
            with mock.patch.object(nasberrypi, "SHARES_FILE", dangling):
                self.assertTrue(nasberrypi.ensure_default_shares_file())
                self.assertTrue(dangling.is_symlink())
                self.assertFalse(dangling.exists())

    def test_ensure_default_shares_file_reports_bootstrap_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi, "save_shares", side_effect=PermissionError("denied")), \
                 mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.ensure_default_shares_file())
            rendered = "\n".join(call.args[0] for call in output.call_args_list)
            self.assertIn("Could not create share configuration", rendered)
            self.assertIn("denied", rendered)
            self.assertFalse(shares_file.exists())

    def test_setup_share_config_preflight_allows_genuinely_missing_file_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                self.assertTrue(nasberrypi.setup_share_config_preflight())
                self.assertFalse(shares_file.exists())

    def test_setup_share_config_preflight_preserves_valid_custom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            original = '{\n  "shares": [{"name": "Media", "path": "Media"}]\n}\n'
            shares_file.write_text(original)
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                self.assertTrue(nasberrypi.setup_share_config_preflight())
                self.assertEqual(shares_file.read_text(), original)

    def test_setup_share_config_preflight_reports_invalid_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text("{broken")
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.setup_share_config_preflight())
            rendered = "\n".join(call.args[0] for call in output.call_args_list)
            self.assertIn("Share configuration error", rendered)
            self.assertIn("invalid JSON", rendered)
            self.assertIn("will not overwrite", rendered)
            self.assertEqual(shares_file.read_text(), "{broken")

    def test_setup_share_config_preflight_accepts_empty_and_all_disabled_configurations(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                shares_file.write_text('{"shares": []}')
                self.assertTrue(nasberrypi.setup_share_config_preflight())
                shares_file.write_text('{"shares": [{"name": "Media", "path": "Media", "enabled": false}]}')
                self.assertTrue(nasberrypi.setup_share_config_preflight())

    def test_setup_share_config_preflight_ignores_underlying_mount_point_symlinks(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            mount = Path(directory) / "nasberry"
            mount.mkdir()
            (mount / "Media").symlink_to(outside, target_is_directory=True)
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text('{"shares": [{"name": "Media", "path": "Media"}]}')
            with mock.patch.object(nasberrypi, "MOUNT_POINT", str(mount)), \
                 mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                self.assertTrue(nasberrypi.setup_share_config_preflight())

    def test_setup_share_config_preflight_rejects_dangling_symlink_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.symlink_to(Path(directory) / "missing-target.json")
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.setup_share_config_preflight())
            rendered = "\n".join(call.args[0] for call in output.call_args_list)
            self.assertIn("Share configuration error", rendered)
            self.assertTrue(shares_file.is_symlink())
            self.assertFalse(shares_file.exists())

    def test_setup_share_config_preflight_reports_directory_and_unreadable_path(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.mkdir()
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), mock.patch("builtins.print"):
                self.assertFalse(nasberrypi.setup_share_config_preflight())
            shares_file.rmdir()
            shares_file.write_text("{}")
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")), \
                 mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch("builtins.print"):
                self.assertFalse(nasberrypi.setup_share_config_preflight())

    def test_share_config_preflight_reports_missing_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.share_config_preflight())
            rendered = "\n".join(call.args[0] for call in output.call_args_list)
            self.assertIn("Share configuration error", rendered)
            self.assertIn("missing", rendered)
            self.assertFalse(shares_file.exists())

    def test_share_config_preflight_reports_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text("{broken")
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.share_config_preflight())
            rendered = "\n".join(call.args[0] for call in output.call_args_list)
            self.assertIn("Share configuration error", rendered)
            self.assertIn("invalid JSON", rendered)

    def test_share_config_preflight_accepts_empty_and_all_disabled_configurations(self):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file):
                shares_file.write_text('{"shares": []}')
                self.assertTrue(nasberrypi.share_config_preflight())
                shares_file.write_text('{"shares": [{"name": "Media", "path": "Media", "enabled": false}]}')
                self.assertTrue(nasberrypi.share_config_preflight())

    @mock.patch.object(nasberrypi, "mount_storage")
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=False)
    @mock.patch.object(nasberrypi, "service_exists", return_value=True)
    def test_start_share_refuses_busy_mount_point_with_wrong_device(self, _service, _nas_mount, _mounted, mount_storage):
        mount_storage.return_value = False
        with mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True):
            self.assertFalse(nasberrypi.start_share())
        mount_storage.assert_called_once_with()

    def test_start_share_refuses_zero_enabled_shares_before_side_effects(self):
        with mock.patch.object(nasberrypi, "enabled_shares", return_value=[]), \
             mock.patch.object(nasberrypi, "service_exists") as service_exists, \
             mock.patch.object(nasberrypi, "share_user_preflight") as user_preflight, \
             mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
             mock.patch.object(nasberrypi, "ensure_public_folder") as ensure_public, \
             mock.patch.object(nasberrypi, "run") as run, \
             mock.patch.object(nasberrypi, "write_state") as write_state, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.start_share())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("No Nasberry shared folders are enabled", rendered)
        self.assertIn("sudo nasberry shares", rendered)
        service_exists.assert_not_called()
        user_preflight.assert_not_called()
        mount_storage.assert_not_called()
        ensure_public.assert_not_called()
        run.assert_not_called()
        write_state.assert_not_called()

    def test_start_share_refuses_broken_share_config_before_mounting(self):
        with mock.patch.object(nasberrypi, "enabled_shares", side_effect=nasberrypi.ShareConfigError("invalid shares")), \
             mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
             mock.patch.object(nasberrypi, "ensure_public_folder") as ensure_public, \
             mock.patch.object(nasberrypi, "run") as run, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.start_share())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Share configuration error: invalid shares", rendered)
        mount_storage.assert_not_called()
        ensure_public.assert_not_called()
        run.assert_not_called()

    def test_mount_storage_refuses_unresolved_mount_ownership_before_side_effects(self):
        with mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"), \
             mock.patch.object(nasberrypi, "storage_mount_options", return_value=None), \
             mock.patch.object(nasberrypi, "ensure_mount_point") as ensure_mount_point, \
             mock.patch.object(nasberrypi, "cleanup_other_mounts") as cleanup_other_mounts, \
             mock.patch.object(nasberrypi, "stop_share") as stop_share, \
             mock.patch.object(nasberrypi, "run") as run, \
             mock.patch.object(nasberrypi, "write_state") as write_state, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.mount_storage())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Cannot mount this filesystem safely", rendered)
        self.assertIn("deleteduser", rendered)
        self.assertIn("sudo nasberry setup", rendered)
        ensure_mount_point.assert_not_called()
        cleanup_other_mounts.assert_not_called()
        stop_share.assert_not_called()
        run.assert_not_called()
        write_state.assert_not_called()

    def test_mount_storage_refuses_empty_mount_owner_before_side_effects(self):
        with mock.patch.object(nasberrypi, "SHARE_USER", ""), \
             mock.patch.object(nasberrypi, "storage_mount_options", return_value=None), \
             mock.patch.object(nasberrypi, "ensure_mount_point") as ensure_mount_point, \
             mock.patch.object(nasberrypi, "cleanup_other_mounts") as cleanup_other_mounts, \
             mock.patch.object(nasberrypi, "write_state") as write_state, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.mount_storage())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("no Linux share user is configured", rendered)
        self.assertIn("sudo nasberry setup", rendered)
        ensure_mount_point.assert_not_called()
        cleanup_other_mounts.assert_not_called()
        write_state.assert_not_called()

    def test_mount_storage_refuses_unresolved_mount_ownership_even_when_already_mounted(self):
        with mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"), \
             mock.patch.object(nasberrypi, "storage_mount_options", return_value=None), \
             mock.patch.object(nasberrypi, "is_mounted", return_value=True) as is_mounted, \
             mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True) as device_mounted, \
             mock.patch.object(nasberrypi, "ensure_mount_point") as ensure_mount_point, \
             mock.patch.object(nasberrypi, "cleanup_other_mounts") as cleanup_other_mounts, \
             mock.patch.object(nasberrypi, "run") as run, \
             mock.patch.object(nasberrypi, "write_state") as write_state, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True))
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertNotIn("already mounted in NAS mode", rendered)
        is_mounted.assert_not_called()
        device_mounted.assert_not_called()
        ensure_mount_point.assert_not_called()
        cleanup_other_mounts.assert_not_called()
        run.assert_not_called()
        write_state.assert_not_called()

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=[])
    @mock.patch.object(nasberrypi, "time")
    @mock.patch.object(nasberrypi, "run")
    def test_mount_storage_unix_permission_filesystem_mounts_without_uid_options(
        self, run, _time, _options, _nas_mount, _device, _mounted, _cleanup, _ensure, _write_state, _service_active
    ):
        run.return_value = mock.Mock(returncode=0, stderr="", stdout="")
        self.assertTrue(nasberrypi.mount_storage())
        run.assert_called_once_with(nasberrypi.sudo_cmd("mount", nasberrypi.DEVICE, nasberrypi.MOUNT_POINT))

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=["-o", "uid=1000,gid=1000,umask=0002"])
    @mock.patch.object(nasberrypi, "time")
    @mock.patch.object(nasberrypi, "run")
    def test_mount_storage_mount_permission_filesystem_uses_uid_options(
        self, run, _time, _options, _nas_mount, _device, _mounted, _cleanup, _ensure, _write_state, _service_active
    ):
        run.return_value = mock.Mock(returncode=0, stderr="", stdout="")
        self.assertTrue(nasberrypi.mount_storage())
        run.assert_called_once_with(
            nasberrypi.sudo_cmd("mount", "-o", "uid=1000,gid=1000,umask=0002", nasberrypi.DEVICE, nasberrypi.MOUNT_POINT)
        )

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=False)
    def test_mount_storage_repair_does_not_unmount_wrong_device(
        self, _nas_mount, _mounted, _cleanup, _ensure, _write_state, _service_active, run
    ):
        with mock.patch.object(nasberrypi, "storage_mount_options", return_value=[]):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True))
        run.assert_not_called()

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts")
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=False)
    def test_mount_storage_checks_occupied_target_before_external_cleanup(
        self, _nas_mount, _mounted, cleanup, _ensure, _write_state, _service_active
    ):
        with mock.patch.object(nasberrypi, "storage_mount_options", return_value=[]):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        cleanup.assert_not_called()

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=False)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    def test_mount_storage_ordinary_mode_keeps_external_move_confirmation(
        self, _mounted, cleanup, _ensure, _write_state, _service_active
    ):
        with mock.patch.object(nasberrypi, "storage_mount_options", return_value=[]):
            self.assertFalse(nasberrypi.mount_storage())
        cleanup.assert_called_once_with(confirm=True)

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=False)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    def test_mount_storage_setup_can_still_disable_external_move_confirmation(
        self, _mounted, cleanup, _ensure, _write_state, _service_active
    ):
        with mock.patch.object(nasberrypi, "storage_mount_options", return_value=[]):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=False))
        cleanup.assert_called_once_with(confirm=False)

    @mock.patch.object(nasberrypi, "device_mount_points", return_value=["/media/foo"])
    def test_cleanup_other_mounts_decline_does_not_unmount(self, _mounts):
        with mock.patch.object(nasberrypi.sys, "stdin", self.FakeTTY("n\n", tty=True)), \
             mock.patch.object(nasberrypi, "run") as run:
            self.assertFalse(nasberrypi.cleanup_other_mounts(confirm=True))
        run.assert_not_called()

    @mock.patch.object(nasberrypi, "device_mount_points", return_value=["/media/foo"])
    def test_cleanup_other_mounts_non_interactive_fails_closed(self, _mounts):
        with mock.patch.object(nasberrypi.sys, "stdin", self.FakeTTY(tty=False)), \
             mock.patch.object(nasberrypi, "run") as run:
            self.assertFalse(nasberrypi.cleanup_other_mounts(confirm=True))
        run.assert_not_called()

    @mock.patch.object(nasberrypi, "device_mount_points", return_value=["/media/foo"])
    def test_cleanup_other_mounts_approval_unmounts_external_location(self, _mounts):
        result = mock.Mock(returncode=0)
        with mock.patch.object(nasberrypi.sys, "stdin", self.FakeTTY("yes\n", tty=True)), \
             mock.patch.object(nasberrypi, "run", return_value=result) as run:
            self.assertTrue(nasberrypi.cleanup_other_mounts(confirm=True))
        run.assert_called_once_with(nasberrypi.sudo_cmd("umount", "/media/foo"))

    @mock.patch.object(nasberrypi, "device_mount_points", return_value=["/media/foo"])
    def test_cleanup_other_mounts_busy_unmount_failure_stops(self, _mounts):
        result = mock.Mock(returncode=1, stderr="busy")
        with mock.patch.object(nasberrypi.sys, "stdin", self.FakeTTY("y\n", tty=True)), \
             mock.patch.object(nasberrypi, "run", return_value=result) as run:
            self.assertFalse(nasberrypi.cleanup_other_mounts(confirm=True))
        run.assert_called_once_with(nasberrypi.sudo_cmd("umount", "/media/foo"))

    @mock.patch.object(nasberrypi, "samba_config_valid", return_value=(True, "ready"))
    @mock.patch.object(nasberrypi, "service_exists", return_value=True)
    @mock.patch.object(nasberrypi, "service_active", side_effect=[True, True])
    @mock.patch.object(nasberrypi, "stop_share", return_value=True)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=["-o", "uid=1000,gid=1000,umask=0002"])
    @mock.patch.object(nasberrypi, "run")
    def test_permission_remount_umount_failure_restores_previous_service_when_safe(
        self, run, _options, _mounted, _nas_mount, _cleanup, _ensure, write_state, _stop, _active, _exists, _valid
    ):
        run.side_effect = [
            mock.Mock(returncode=1, stderr="busy"),
            mock.Mock(returncode=0, stderr=""),
        ]
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        self.assertEqual(run.call_args_list[0].args[0], nasberrypi.sudo_cmd("umount", nasberrypi.MOUNT_POINT))
        self.assertEqual(run.call_args_list[1].args[0], nasberrypi.sudo_cmd("systemctl", "start", nasberrypi.SAMBA_SERVICE))
        write_state.assert_called_with(True, True)

    @mock.patch.object(nasberrypi, "samba_config_valid", return_value=(False, "invalid"))
    @mock.patch.object(nasberrypi, "service_exists", return_value=True)
    @mock.patch.object(nasberrypi, "service_active", side_effect=[True, False])
    @mock.patch.object(nasberrypi, "stop_share", return_value=True)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=["-o", "uid=1000,gid=1000,umask=0002"])
    @mock.patch.object(nasberrypi, "run")
    def test_permission_remount_umount_failure_does_not_restore_with_invalid_samba_config(
        self, run, _options, _mounted, _nas_mount, _cleanup, _ensure, write_state, _stop, _active, _exists, _valid
    ):
        run.return_value = mock.Mock(returncode=1, stderr="busy")
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        run.assert_called_once_with(nasberrypi.sudo_cmd("umount", nasberrypi.MOUNT_POINT))
        write_state.assert_called_with(True, False)

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "service_exists", return_value=False)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    def test_failed_remount_restoration_skips_missing_service(self, _nas_mount, _exists, _mounted, write_state, run):
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.restore_share_after_failed_remount(True))
        run.assert_not_called()
        write_state.assert_called_once_with(True, False)

    @mock.patch.object(nasberrypi, "service_active", side_effect=[True, False, False])
    @mock.patch.object(nasberrypi, "stop_share", return_value=True)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", side_effect=[True, False, False])
    @mock.patch.object(nasberrypi, "is_mounted", side_effect=[True, True, False, False])
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=["-o", "uid=1000,gid=1000,umask=0002"])
    @mock.patch.object(nasberrypi, "time")
    @mock.patch.object(nasberrypi, "run")
    def test_permission_remount_mount_failure_does_not_restore_without_safe_storage(
        self, run, _time, _options, _device, _mounted, _nas_mount, _cleanup, _ensure, write_state, _stop, _active
    ):
        run.side_effect = [
            mock.Mock(returncode=0, stderr="", stdout=""),
            mock.Mock(returncode=1, stderr="mount failed", stdout=""),
        ]
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        self.assertEqual(run.call_args_list[0].args[0], nasberrypi.sudo_cmd("umount", nasberrypi.MOUNT_POINT))
        self.assertEqual(
            run.call_args_list[1].args[0],
            nasberrypi.sudo_cmd("mount", "-o", "uid=1000,gid=1000,umask=0002", nasberrypi.DEVICE, nasberrypi.MOUNT_POINT),
        )
        self.assertEqual(len(run.call_args_list), 2)
        write_state.assert_called_with(False, False)

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "stop_share")
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=["-o", "uid=1000,gid=1000,umask=0002"])
    @mock.patch.object(nasberrypi, "run")
    def test_inactive_service_before_remount_failure_is_not_started(
        self, run, _options, _mounted, _nas_mount, _cleanup, _ensure, _write_state, stop_share, _active
    ):
        run.return_value = mock.Mock(returncode=1, stderr="busy")
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        stop_share.assert_not_called()
        run.assert_called_once_with(nasberrypi.sudo_cmd("umount", nasberrypi.MOUNT_POINT))

    @mock.patch.object(nasberrypi, "service_active", return_value=True)
    @mock.patch.object(nasberrypi, "stop_share", return_value=False)
    @mock.patch.object(nasberrypi, "restore_share_after_failed_remount")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=["-o", "uid=1000,gid=1000,umask=0002"])
    @mock.patch.object(nasberrypi, "run")
    def test_stop_share_failure_does_not_attempt_restoration(
        self, run, _options, _mounted, _nas_mount, _cleanup, _ensure, restore, _stop, _active
    ):
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        run.assert_not_called()
        restore.assert_not_called()

    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False}])
    @mock.patch.object(nasberrypi, "run")
    def test_configure_samba_keeps_live_config_when_candidate_is_invalid(self, run, _load_shares, _preflight):
        run.return_value.returncode = 1
        run.return_value.stderr = "invalid"
        run.return_value.stdout = ""
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text("original config")
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), \
                 mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
                 mock.patch.object(nasberrypi, "share_user_preflight", return_value=True):
                self.assertFalse(nasberrypi.configure_samba_share())
            self.assertEqual(smb_file.read_text(), "original config")

    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": "/mnt/nasberry/Public", "enabled": True, "read_only": False}])
    @mock.patch.object(nasberrypi, "run")
    def test_configure_samba_candidate_cleanup_failure_does_not_mask_rejection(self, run, _load_shares, _preflight):
        run.return_value.returncode = 1
        run.return_value.stderr = "candidate invalid"
        run.return_value.stdout = ""
        original_unlink = Path.unlink
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text("original config")

            def unlink(path, *args, **kwargs):
                if path.parent == smb_file.parent and path.name.startswith(".smb.conf.nasberry."):
                    raise PermissionError("cleanup failure")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), \
                 mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
                 mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
                 mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink), \
                 mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.configure_samba_share())
            self.assertEqual(smb_file.read_text(), "original config")
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Samba config validation failed: candidate invalid", rendered)

    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "samba_shares", return_value={"OtherShare": {"path": "/srv/other"}})
    @mock.patch.object(nasberrypi, "load_shares", return_value=[
        {"name": "Public", "path": "/mnt/nasberry/Public", "enabled": False, "read_only": False}
    ])
    @mock.patch.object(nasberrypi, "run")
    def test_configure_samba_writes_empty_managed_section_for_zero_enabled(self, run, _load_shares, _samba_shares, _preflight):
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        run.return_value.stdout = ""
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text(
                f"""[global]
   workgroup = WORKGROUP

[OtherShare]
   path = /srv/other

{nasberrypi.NASBERRY_SAMBA_BEGIN}
# Managed by NasberryPi. Edit with 'sudo nasberry shares'.
[Public]
   path = /mnt/nasberry/Public
{nasberrypi.NASBERRY_SAMBA_END}
"""
            )
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), \
                 mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
                 mock.patch.object(nasberrypi, "share_user_preflight", return_value=True):
                self.assertTrue(nasberrypi.configure_samba_share())
            updated = smb_file.read_text()
            self.assertIn("[OtherShare]", updated)
            self.assertIn(nasberrypi.NASBERRY_SAMBA_BEGIN, updated)
            self.assertIn(nasberrypi.NASBERRY_SAMBA_END, updated)
            managed = updated.split(nasberrypi.NASBERRY_SAMBA_BEGIN, 1)[1].split(nasberrypi.NASBERRY_SAMBA_END, 1)[0]
            self.assertNotIn("[Public]", managed)
            self.assertTrue(list(Path(directory).glob("smb.conf.nasberry.*.bak")))
        self.assertEqual(run.call_args.args[0][:2], ["testparm", "-s"])

    @mock.patch.object(nasberrypi, "share_user_preflight")
    @mock.patch.object(nasberrypi, "samba_config_preflight")
    def test_configure_samba_rejects_unsafe_user_before_preflight(self, preflight, user_preflight):
        with mock.patch.object(nasberrypi, "SHARE_USER", "user\nadmin users = root"):
            self.assertFalse(nasberrypi.configure_samba_share())
        user_preflight.assert_not_called()
        preflight.assert_not_called()

    @mock.patch.object(nasberrypi, "samba_config_valid")
    @mock.patch.object(nasberrypi, "samba_config_preflight")
    @mock.patch.object(nasberrypi, "appliance_samba_config")
    @mock.patch.object(nasberrypi, "run")
    def test_configure_samba_refuses_missing_linux_user_before_modifying_samba(self, run, appliance, preflight, config_valid):
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text("original config")
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), \
                 mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"), \
                 mock.patch.object(nasberrypi.pwd, "getpwnam", side_effect=KeyError), \
                 mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.configure_samba_share())
            self.assertEqual(smb_file.read_text(), "original config")
            self.assertEqual(list(Path(directory).glob("smb.conf.nasberry.*.bak")), [])
            self.assertEqual(list(Path(directory).glob(".smb.conf.nasberry.*")), [])
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Configured Linux user does not exist", rendered)
        self.assertIn("sudo nasberry setup", rendered)
        appliance.assert_not_called()
        preflight.assert_not_called()
        run.assert_not_called()
        config_valid.assert_not_called()

    @mock.patch.object(nasberrypi, "samba_config_preflight")
    @mock.patch.object(nasberrypi, "load_shares", side_effect=nasberrypi.ShareConfigError("invalid shares"))
    def test_configure_samba_refuses_broken_share_config_before_modifying_samba(self, load_shares, preflight):
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text("original config")
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), \
                 mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
                 mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
                 mock.patch("builtins.print"):
                self.assertFalse(nasberrypi.configure_samba_share())
            self.assertEqual(smb_file.read_text(), "original config")
            self.assertEqual(list(Path(directory).glob("smb.conf.nasberry.*.bak")), [])
        load_shares.assert_called_once_with()
        preflight.assert_not_called()

    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=False)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    def test_storage_layout_creates_and_protects_posix_folders(self, _nas_mount, _mount_permissions):
        with tempfile.TemporaryDirectory() as directory:
            private_file = Path(directory) / "Private" / "keep.txt"
            private_file.parent.mkdir()
            private_file.write_text("preserve me")
            owner = mock.Mock(pw_uid=Path(directory).stat().st_uid, pw_gid=Path(directory).stat().st_gid)
            with mock.patch.object(nasberrypi, "MOUNT_POINT", directory), mock.patch.object(nasberrypi, "SHARE_USER", "kali"), mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=owner):
                self.assertTrue(nasberrypi.ensure_storage_layout())
            self.assertEqual((Path(directory) / "Public").stat().st_mode & 0o777, 0o775)
            self.assertEqual((Path(directory) / "Private").stat().st_mode & 0o777, 0o700)
            self.assertEqual((Path(directory) / "Backups").stat().st_mode & 0o777, 0o700)
            self.assertEqual(private_file.read_text(), "preserve me")

    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=False)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    def test_storage_layout_rejects_symlink_without_touching_target(self, _nas_mount, _mount_permissions):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as target:
            (Path(directory) / "Private").symlink_to(target, target_is_directory=True)
            owner = mock.Mock(pw_uid=Path(directory).stat().st_uid, pw_gid=Path(directory).stat().st_gid)
            with mock.patch.object(nasberrypi, "MOUNT_POINT", directory), mock.patch.object(nasberrypi, "SHARE_USER", "kali"), mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=owner):
                self.assertFalse(nasberrypi.ensure_storage_layout())
            self.assertEqual(list(Path(target).iterdir()), [])

    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    def test_folder_preparation_keeps_downstream_missing_user_defense(self, _nas_mount):
        with mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"), \
             mock.patch.object(nasberrypi.pwd, "getpwnam", side_effect=KeyError), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.ensure_storage_layout())
            self.assertFalse(nasberrypi.ensure_share_folders())

    @mock.patch.object(nasberrypi, "storage_filesystem", return_value="exfat")
    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    def test_protected_folder_reports_exfat_limitation(self, _nas_mount, _is_mounted, _mount_permissions, _filesystem):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Private").mkdir()
            with mock.patch.object(nasberrypi, "MOUNT_POINT", directory):
                valid, detail = nasberrypi.protected_folder_status("Private")
            self.assertTrue(valid)
            self.assertIn("exfat", detail)
            self.assertIn("local-only", detail)

    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=False)
    def test_ensure_share_folders_requires_configured_device_at_mount_point(self, _nas_mount, _is_mounted):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            self.assertFalse(nasberrypi.ensure_share_folders())

    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "load_shares", side_effect=nasberrypi.ShareConfigError("invalid shares"))
    def test_ensure_share_folders_refuses_broken_share_config(self, load_shares, _nas_mount):
        owner = mock.Mock(pw_uid=1000, pw_gid=1000)
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=owner), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.ensure_share_folders())
        load_shares.assert_called_once_with()

    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    def test_share_folders_reject_mount_permission_owner_mismatch(self, _nas_mount, _is_mounted, _mount_permissions):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "Public"
            public.mkdir()
            owner = mock.Mock(pw_uid=public.stat().st_uid + 1, pw_gid=public.stat().st_gid + 1)
            with mock.patch.object(nasberrypi, "MOUNT_POINT", directory), \
                 mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
                 mock.patch.object(nasberrypi, "load_shares", return_value=[{"name": "Public", "path": str(public), "enabled": True, "read_only": False}]), \
                 mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=owner):
                self.assertFalse(nasberrypi.ensure_share_folders())

    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    def test_public_folder_access_skips_check_while_storage_is_unmounted(self, _is_mounted):
        self.assertEqual(
            nasberrypi.public_folder_access_valid(),
            (True, "not checked while storage is safely unmounted"),
        )

    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    def test_public_folder_access_reports_configured_user_write_access(self, _nas_mount, _is_mounted):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "Public"
            public.mkdir()
            owner = mock.Mock(pw_uid=public.stat().st_uid)
            with mock.patch.object(nasberrypi, "public_share_path", return_value=str(public)), mock.patch.object(nasberrypi, "SHARE_USER", "kali"), mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=owner):
                self.assertEqual(nasberrypi.public_folder_access_valid(), (True, "writable by kali"))

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    def test_samba_account_reports_enabled_user(self, _command_exists, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "Unix username: kali\nAccount Flags: [U          ]\n"
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            self.assertEqual(nasberrypi.samba_account_valid(), (True, "enabled for kali"))
        run.assert_called_once_with(["pdbedit", "-L", "-v", "kali"])

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    def test_samba_account_reports_disabled_user(self, _command_exists, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "Account Flags: [DU         ]\n"
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            valid, detail = nasberrypi.samba_account_valid()
        self.assertFalse(valid)
        self.assertIn("disabled", detail)

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    def test_setup_preflight_rejects_device_without_uuid(self, _mounted, _command, _geteuid):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "device"
            device.touch()
            smb_file = Path(directory) / "smb.conf"
            smb_file.touch()
            mount_point = Path(directory) / "mount"
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), mock.patch.object(nasberrypi, "MOUNT_POINT", str(mount_point)), mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=mock.Mock()):
                self.assertFalse(nasberrypi.setup_preflight({"path": str(device), "uuid": ""}, "kali"))

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    def test_setup_preflight_rejects_nonempty_unmounted_mount_point(self, _mounted, _command, _geteuid):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "device"
            device.touch()
            smb_file = Path(directory) / "smb.conf"
            smb_file.touch()
            mount_point = Path(directory) / "mount"
            mount_point.mkdir()
            (mount_point / "unexpected-file").touch()
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), mock.patch.object(nasberrypi, "MOUNT_POINT", str(mount_point)), mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=mock.Mock()):
                self.assertFalse(nasberrypi.setup_preflight({"path": str(device), "uuid": "uuid"}, "kali"))

    def test_panel_adapts_to_small_terminal_width(self):
        rendered = nasberrypi.panel("STATUS", ["A long status message that must wrap cleanly"], width=24)
        self.assertTrue(all(len(line) == 24 for line in rendered))
        self.assertGreater(len(rendered), 3)

    def test_log_colorizes_status_symbol_when_color_is_enabled(self):
        with mock.patch.object(nasberrypi, "color_enabled", return_value=True), mock.patch("builtins.print") as output:
            nasberrypi.log("✔ Sharing online")
        self.assertIn("\033[1;32m✔ Sharing online\033[0m", output.call_args.args[0])

    def test_doctor_groups_checks_and_emphasizes_result(self):
        with mock.patch.object(nasberrypi, "check", return_value=True), \
             mock.patch.object(nasberrypi, "device_exists", return_value=True), \
             mock.patch.object(nasberrypi, "storage_mount_state", return_value="safely_unmounted"), \
             mock.patch.object(nasberrypi, "active_mount_point", return_value=""), \
             mock.patch.object(nasberrypi, "public_folder_access_valid", return_value=(True, "ready")), \
             mock.patch.object(nasberrypi, "protected_folder_status", return_value=(True, "protected")), \
             mock.patch.object(nasberrypi, "samba_config_valid", return_value=(True, "ready")), \
             mock.patch.object(nasberrypi, "samba_account_valid", return_value=(True, "enabled")), \
             mock.patch.object(nasberrypi, "load_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "print_connection_info"), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False), \
             mock.patch("builtins.print") as output:
            self.assertTrue(nasberrypi.doctor())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        for section in ("SYSTEM", "STORAGE", "SAMBA", "NETWORK", "RESULT"):
            self.assertIn(section, rendered)
        self.assertIn("✔ Result: 22/22 checks passed", rendered)

    def test_doctor_reports_broken_share_configuration_without_raising(self):
        checks = []

        def fake_check(label, ok, detail, fix="", label_width=22):
            checks.append((label, ok, detail, fix))
            return ok

        with mock.patch.object(nasberrypi, "check", side_effect=fake_check), \
             mock.patch.object(nasberrypi, "section_header"), \
             mock.patch.object(nasberrypi.os.path, "isdir", return_value=True), \
             mock.patch.object(nasberrypi, "device_exists", return_value=True), \
             mock.patch.object(nasberrypi, "storage_mount_state", return_value="safely_unmounted"), \
             mock.patch.object(nasberrypi, "active_mount_point", return_value=""), \
             mock.patch.object(nasberrypi, "protected_folder_status", return_value=(True, "protected")), \
             mock.patch.object(nasberrypi, "samba_config_valid", return_value=(False, "invalid shares")), \
             mock.patch.object(nasberrypi, "samba_account_valid", return_value=(True, "enabled")), \
             mock.patch.object(nasberrypi, "load_shares", side_effect=nasberrypi.ShareConfigError("invalid JSON in /tmp/shares.json")), \
             mock.patch.object(nasberrypi, "command_exists", return_value=True), \
             mock.patch.object(nasberrypi.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(nasberrypi, "service_exists", return_value=True), \
             mock.patch.object(nasberrypi, "print_connection_info"), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.doctor())
        share_check = next(item for item in checks if item[0] == "Share configuration")
        self.assertFalse(share_check[1])
        self.assertIn("invalid JSON", share_check[2])
        self.assertIn(str(nasberrypi.SHARES_FILE), share_check[3])

    def test_doctor_treats_zero_enabled_shares_as_valid_configuration(self):
        checks = []

        def fake_check(label, ok, detail, fix="", label_width=22):
            checks.append((label, ok, detail, fix))
            return ok

        with mock.patch.object(nasberrypi, "check", side_effect=fake_check), \
             mock.patch.object(nasberrypi, "section_header"), \
             mock.patch.object(nasberrypi.os.path, "isdir", return_value=True), \
             mock.patch.object(nasberrypi, "device_exists", return_value=True), \
             mock.patch.object(nasberrypi, "storage_mount_state", return_value="safely_unmounted"), \
             mock.patch.object(nasberrypi, "active_mount_point", return_value=""), \
             mock.patch.object(nasberrypi, "protected_folder_status", return_value=(True, "protected")), \
             mock.patch.object(nasberrypi, "samba_config_valid", return_value=(True, "0 enabled share(s) configured")), \
             mock.patch.object(nasberrypi, "samba_account_valid", return_value=(True, "enabled")), \
             mock.patch.object(nasberrypi, "load_shares", return_value=[]), \
             mock.patch.object(nasberrypi, "command_exists", return_value=True), \
             mock.patch.object(nasberrypi.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(nasberrypi, "service_exists", return_value=True), \
             mock.patch.object(nasberrypi, "print_connection_info"), \
             mock.patch("builtins.print"):
            self.assertTrue(nasberrypi.doctor())
        share_check = next(item for item in checks if item[0] == "Share configuration")
        self.assertTrue(share_check[1])
        self.assertIn("0 enabled of 0 configured", share_check[2])
        self.assertIn("no Nasberry shares enabled", share_check[2])

    def test_doctor_reports_missing_share_configuration_with_setup_guidance(self):
        checks = []

        def fake_check(label, ok, detail, fix="", label_width=22):
            checks.append((label, ok, detail, fix))
            return ok

        with mock.patch.object(nasberrypi, "check", side_effect=fake_check), \
             mock.patch.object(nasberrypi, "section_header"), \
             mock.patch.object(nasberrypi, "device_exists", return_value=True), \
             mock.patch.object(nasberrypi, "storage_mount_state", return_value="safely_unmounted"), \
             mock.patch.object(nasberrypi, "active_mount_point", return_value=""), \
             mock.patch.object(nasberrypi, "protected_folder_status", return_value=(True, "protected")), \
             mock.patch.object(nasberrypi, "samba_config_valid", return_value=(False, "missing shares")), \
             mock.patch.object(nasberrypi, "samba_account_valid", return_value=(True, "enabled")), \
             mock.patch.object(nasberrypi, "load_shares", side_effect=nasberrypi.ShareConfigError("share configuration file is missing: /tmp/shares.json")), \
             mock.patch.object(nasberrypi, "command_exists", return_value=True), \
             mock.patch.object(nasberrypi.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(nasberrypi, "service_exists", return_value=True), \
             mock.patch.object(nasberrypi, "print_connection_info"), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.doctor())
        share_check = next(item for item in checks if item[0] == "Share configuration")
        self.assertIn("missing", share_check[2])
        self.assertIn("sudo nasberry setup", share_check[3])

    def test_doctor_reports_invalid_main_configuration_without_storage_probes(self):
        checks = []

        def fake_check(label, ok, detail, fix="", label_width=22):
            checks.append((label, ok, detail, fix))
            return ok

        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "invalid safe_mode_on_start"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "check", side_effect=fake_check), \
             mock.patch.object(nasberrypi, "section_header"), \
             mock.patch.object(nasberrypi, "device_exists") as device_exists, \
             mock.patch.object(nasberrypi, "command_exists", return_value=True), \
             mock.patch.object(nasberrypi.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(nasberrypi, "print_connection_info") as connection_info, \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.doctor())
        config_check = next(item for item in checks if item[0] == "Configuration")
        self.assertFalse(config_check[1])
        self.assertIn("invalid safe_mode_on_start", config_check[2])
        device_exists.assert_not_called()
        connection_info.assert_not_called()

    def test_dashboard_action_suppresses_nested_operation_header(self):
        with mock.patch("builtins.print") as output:
            nasberrypi.run_dashboard_action(
                lambda: nasberrypi.operation_header("MOUNT STORAGE", "Preparing storage for NAS access")
            )
        output.assert_not_called()
        self.assertNotIn("dashboard_action", nasberrypi.state)

    def test_dashboard_action_suppresses_diagnostics_panel(self):
        with mock.patch.object(nasberrypi, "section_header"), \
             mock.patch.object(nasberrypi, "check", return_value=True), \
             mock.patch.object(nasberrypi, "storage_mount_state", return_value="safely_unmounted"), \
             mock.patch.object(nasberrypi, "active_mount_point", return_value=""), \
             mock.patch.object(nasberrypi, "public_folder_access_valid", return_value=(True, "ready")), \
             mock.patch.object(nasberrypi, "protected_folder_status", return_value=(True, "protected")), \
             mock.patch.object(nasberrypi, "samba_config_valid", return_value=(True, "ready")), \
             mock.patch.object(nasberrypi, "samba_account_valid", return_value=(True, "enabled")), \
             mock.patch.object(nasberrypi, "load_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "print_connection_info"), \
             mock.patch("builtins.print") as output:
            nasberrypi.run_dashboard_action(nasberrypi.doctor)
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertNotIn("DIAGNOSTICS", rendered)
        self.assertIn("Result:", rendered)

    def test_clear_uses_visible_screen_only_sequence(self):
        output = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.sys, "stdout", output), \
             mock.patch.object(nasberrypi.os, "system") as system:
            nasberrypi.clear()
        self.assertEqual("".join(output.writes), "\033[H\033[2J\033[H")
        self.assertNotIn("\033[3J", "".join(output.writes))
        self.assertTrue(output.flushed)
        system.assert_not_called()

    def test_clear_non_tty_does_not_emit_ansi_or_shell_out(self):
        output = self.FakeOutput(tty=False)
        with mock.patch.object(nasberrypi.sys, "stdout", output), \
             mock.patch.object(nasberrypi.os, "system") as system:
            nasberrypi.clear()
        self.assertEqual(output.writes, [])
        system.assert_not_called()

    def test_action_feedback_uses_visible_screen_clear_without_scrollback_delete(self):
        output_stream = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.sys, "stdout", output_stream), \
             mock.patch.object(nasberrypi.os, "system") as system, \
             mock.patch("builtins.print") as output:
            nasberrypi.show_action_feedback("Setup / change drive", "prompt")
        self.assertEqual(output.call_count, 1)
        self.assertEqual(output_stream.writes, ["\033[H\033[2J\033[H"])
        self.assertNotIn("\033[3J", "".join(output_stream.writes))
        system.assert_not_called()

    def test_menu_exit_uses_visible_screen_clear_without_scrollback_delete(self):
        output_stream = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.sys, "stdout", output_stream), \
             mock.patch.object(nasberrypi.os, "system") as system, \
             mock.patch("builtins.print") as output:
            nasberrypi.show_menu_exit()
        self.assertGreaterEqual(output.call_count, 1)
        self.assertEqual(output_stream.writes, ["\033[H\033[2J\033[H"])
        self.assertNotIn("\033[3J", "".join(output_stream.writes))
        system.assert_not_called()

    def test_draw_screen_tty_clears_home_and_does_not_append_newline(self):
        class Output:
            def __init__(self):
                self.writes = []
                self.flushed = False

            def isatty(self):
                return True

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                self.flushed = True

        output = Output()
        with mock.patch.object(nasberrypi.sys, "stdout", output):
            nasberrypi.draw_screen("frame")
        self.assertEqual("".join(output.writes), "\033[H\033[2J\033[Hframe")
        self.assertTrue(output.flushed)

    def test_dashboard_fits_80_by_24_terminal(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(self.dashboard_actions())
            width = nasberrypi.terminal_width()
        lines = rendered.splitlines()
        self.assertLessEqual(len(lines), 24)
        self.assertEqual(width, 79)
        self.assertTrue(all(len(line) <= width for line in lines))

    def test_dashboard_fits_80_by_25_terminal(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 25))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(self.dashboard_actions())
        lines = rendered.splitlines()
        self.assertLessEqual(len(lines), 25)
        self.assertNotEqual(lines[3], "")

    def test_dashboard_fits_80_by_26_terminal(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 26))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(self.dashboard_actions())
        lines = rendered.splitlines()
        self.assertLessEqual(len(lines), 26)
        self.assertNotEqual(lines[3], "")

    def test_dashboard_uses_full_layout_when_it_fits_80_by_27_terminal(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 27))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            lines = nasberrypi.render_menu(self.dashboard_actions()).splitlines()
        self.assertEqual(len(lines), 27)
        self.assertEqual(lines[3], "")
        self.assertEqual(lines[12], "")
        self.assertEqual(lines[25], "")

    def test_dashboard_keeps_full_spacing_on_larger_terminal(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((100, 40))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            lines = nasberrypi.render_menu(self.dashboard_actions()).splitlines()
        self.assertEqual(len(lines), 27)
        self.assertEqual(lines[3], "")
        self.assertEqual(lines[12], "")
        self.assertEqual(lines[25], "")

    def test_wrapping_full_dashboard_falls_back_when_too_tall(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((45, 27))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(self.dashboard_actions())
            width = nasberrypi.terminal_width()
        lines = rendered.splitlines()
        self.assertLessEqual(len(lines), 27)
        self.assertTrue(all(len(line) <= width for line in lines))
        self.assertNotIn("", lines)

    def test_render_menu_uses_one_status_snapshot_for_compact_fallback(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()) as status, \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(self.dashboard_actions())
        self.assertLessEqual(len(rendered.splitlines()), 24)
        status.assert_called_once_with()

    def test_dashboard_status_cache_reuses_lines_inside_ttl(self):
        cache = nasberrypi.DashboardStatusCache()
        with mock.patch.object(nasberrypi.time, "monotonic", side_effect=[0.0, 0.25, 0.75]), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=["cached"]) as status:
            self.assertEqual(cache.get(), ["cached"])
            self.assertEqual(cache.get(), ["cached"])
            self.assertEqual(cache.get(), ["cached"])
        status.assert_called_once_with()

    def test_dashboard_status_cache_refreshes_after_ttl(self):
        cache = nasberrypi.DashboardStatusCache()
        with mock.patch.object(nasberrypi.time, "monotonic", side_effect=[0.0, 0.5, 1.0, 1.5]), \
             mock.patch.object(nasberrypi, "menu_status_lines", side_effect=[["first"], ["second"]]) as status:
            self.assertEqual(cache.get(), ["first"])
            self.assertEqual(cache.get(), ["first"])
            self.assertEqual(cache.get(), ["second"])
            self.assertEqual(cache.get(), ["second"])
        self.assertEqual(status.call_count, 2)

    def test_cached_status_does_not_block_layout_changes(self):
        cache = nasberrypi.DashboardStatusCache()
        with mock.patch.object(nasberrypi.time, "monotonic", return_value=0.0), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()) as status, \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            status_lines = cache.get()
            with mock.patch.object(nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 27))):
                full = nasberrypi.render_menu(self.dashboard_actions(), status_lines=status_lines).splitlines()
            with mock.patch.object(nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))):
                compact = nasberrypi.render_menu(self.dashboard_actions(), status_lines=status_lines).splitlines()
        status.assert_called_once_with()
        self.assertEqual(len(full), 27)
        self.assertIn("", full)
        self.assertLessEqual(len(compact), 24)
        self.assertNotIn("", compact)

    def test_compact_dashboard_truncates_long_status_without_exceeding_rows(self):
        long_status = self.dashboard_status_lines()
        long_status[2] = "Mount point  /mnt/nasberry/" + "very-long-folder-name-" * 8
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=long_status), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(self.dashboard_actions())
            width = nasberrypi.terminal_width()
        lines = rendered.splitlines()
        self.assertLessEqual(len(lines), 24)
        self.assertTrue(all(len(line) <= width for line in lines))
        self.assertIn("…", rendered)

    def test_narrow_dashboard_uses_minimal_layout_with_safe_line_widths(self):
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((30, 12))
        )
        with terminal, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(self.dashboard_actions(), selected=3)
            width = nasberrypi.terminal_width()
        lines = rendered.splitlines()
        self.assertLessEqual(len(lines), 12)
        self.assertEqual(width, 29)
        self.assertTrue(all(len(line) <= width for line in lines))
        self.assertIn("NASBERRY", rendered)
        self.assertIn("❯ 4", rendered)
        self.assertIn("Q Exit", rendered)

    @mock.patch.object(nasberrypi, "menu_status_lines", return_value=["Storage ready"])
    @mock.patch.object(nasberrypi, "terminal_width", return_value=60)
    def test_menu_presentation_centers_branding_and_shows_shortcuts(self, _width, _status):
        actions = {"1": ("Start sharing files", None), "2": ("Stop sharing files", None)}
        terminal = mock.patch.object(
            nasberrypi.shutil, "get_terminal_size", return_value=os.terminal_size((60, 24))
        )
        with terminal, mock.patch.object(nasberrypi, "color_enabled", return_value=False):
            rendered = nasberrypi.render_menu(actions)
        lines = rendered.splitlines()
        self.assertEqual(lines[0], "NASBERRY".center(60))
        self.assertEqual(lines[1], f"VERSION {nasberrypi.APP_VERSION}".center(60))
        self.assertIn("❯  1   Start sharing files", rendered)
        self.assertIn("Q   Exit", rendered)

    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    @mock.patch.object(nasberrypi, "device_mount_points", return_value=["/mnt/nasberry"])
    def test_menu_mount_status_detects_configured_mount_point(self, _mounts, _mounted):
        with mock.patch.object(nasberrypi, "MOUNT_POINT", "/mnt/nasberry"):
            self.assertEqual(nasberrypi.menu_mount_status(), ("● mounted in NAS mode", "/mnt/nasberry"))

    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    @mock.patch.object(nasberrypi, "device_mount_points", return_value=["/media/user/storage"])
    def test_menu_mount_status_reports_mount_outside_nas_mode(self, _mounts, _mounted):
        with mock.patch.object(nasberrypi, "MOUNT_POINT", "/mnt/nasberry"):
            self.assertEqual(
                nasberrypi.menu_mount_status(),
                ("● mounted elsewhere", "/media/user/storage"),
            )

    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    @mock.patch.object(nasberrypi, "device_mount_points", return_value=[])
    def test_menu_mount_status_marks_unmounted_path_as_configured(self, _mounts, _mounted):
        with mock.patch.object(nasberrypi, "MOUNT_POINT", "/mnt/nasberry"):
            self.assertEqual(
                nasberrypi.menu_mount_status(),
                ("○ safely unmounted", "/mnt/nasberry (configured)"),
            )

    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mount_points", return_value=[])
    def test_menu_mount_status_falls_back_when_device_detection_is_unavailable(self, _mounts, _mounted):
        with mock.patch.object(nasberrypi, "MOUNT_POINT", "/mnt/nasberry"):
            self.assertEqual(nasberrypi.menu_mount_status(), ("● mounted in NAS mode", "/mnt/nasberry"))

    @mock.patch.object(nasberrypi, "disk_usage", return_value="10 GB free of 20 GB")
    @mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()])
    @mock.patch.object(nasberrypi, "service_active", return_value=True)
    @mock.patch.object(nasberrypi, "menu_mount_status", return_value=("● mounted in NAS mode", "/mnt/nasberry"))
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    def test_menu_status_shows_multiple_shared_folders(self, _device, _mount, _sharing, _enabled, _usage):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            rendered = "\n".join(nasberrypi.menu_status_lines())
        self.assertIn("1 enabled share(s)", rendered)
        self.assertIn("Mount point  /mnt/nasberry", rendered)
        self.assertIn("Share user   kali", rendered)
        self.assertIn("Multiple shared folders", rendered)

    @mock.patch.object(nasberrypi, "disk_usage", return_value="10 GB free of 20 GB")
    @mock.patch.object(nasberrypi, "enabled_shares", side_effect=nasberrypi.ShareConfigError("invalid shares"))
    @mock.patch.object(nasberrypi, "service_active", return_value=True)
    @mock.patch.object(nasberrypi, "menu_mount_status", return_value=("● mounted in NAS mode", "/mnt/nasberry"))
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    def test_menu_status_reports_broken_share_config_without_inventing_public(self, _device, _mount, _sharing, _enabled, _usage):
        rendered = "\n".join(nasberrypi.menu_status_lines())
        self.assertIn("share config error", rendered)
        self.assertNotIn("1 enabled share(s)", rendered)

    @mock.patch.object(nasberrypi, "disk_usage", return_value="10 GB free of 20 GB")
    @mock.patch.object(nasberrypi, "enabled_shares", return_value=[])
    @mock.patch.object(nasberrypi, "service_active", return_value=True)
    @mock.patch.object(nasberrypi, "menu_mount_status", return_value=("● mounted in NAS mode", "/mnt/nasberry"))
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    def test_menu_status_zero_enabled_does_not_claim_sharing_online(self, _device, _mount, service_active, _enabled, _usage):
        rendered = "\n".join(nasberrypi.menu_status_lines())
        self.assertIn("no shares enabled", rendered)
        self.assertIn("0 enabled share(s)", rendered)
        self.assertNotIn("sharing online", rendered)
        service_active.assert_not_called()

    def test_menu_status_reports_missing_main_config_without_defaults(self):
        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "not configured"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "device_exists") as device_exists:
            rendered = "\n".join(nasberrypi.menu_status_lines())
        device_exists.assert_not_called()
        self.assertIn("Configuration ○ not configured", rendered)
        self.assertNotIn(nasberrypi.DEFAULTS["device"], rendered)

    def test_tty_navigation_mode_stays_active_across_repeated_down_keys(self):
        selections = []

        def render(_actions, selected=0, _status_lines=None):
            selections.append(selected)
            return "dashboard"

        stdin = self.FakeTTY("\x1b[B\x1b[B\x1b[Bq")
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]) as getattrs, \
             mock.patch.object(nasberrypi.tty, "setcbreak") as setcbreak, \
             mock.patch.object(nasberrypi.termios, "tcsetattr") as setattrs, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()) as status, \
             mock.patch.object(nasberrypi, "render_menu", side_effect=render), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()

        self.assertEqual(selections, [0, 1, 2, 3])
        getattrs.assert_called_once_with(7)
        setcbreak.assert_called_once_with(7, nasberrypi.termios.TCSANOW)
        setattrs.assert_called_once_with(7, nasberrypi.termios.TCSAFLUSH, ["original"])
        status.assert_called_once_with()

    def test_tty_up_navigation_wraps_to_exit(self):
        selections = []

        def render(_actions, selected=0, _status_lines=None):
            selections.append(selected)
            return "dashboard"

        stdin = self.FakeTTY("\x1b[Aq")
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", side_effect=render), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()

        self.assertEqual(selections, [0, 9])

    def test_dashboard_navigation_uses_one_alternate_screen_session(self):
        stdin = self.FakeTTY("\x1b[B\x1b[Bq")
        output = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.sys, "stdout", output), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
        self.assertEqual(output.writes.count("\033[?1049h"), 1)
        self.assertEqual(output.writes.count("\033[?1049l"), 1)

    def test_clean_exit_feedback_runs_after_leaving_alternate_screen(self):
        events = []
        stdin = self.FakeTTY("q")
        output = self.FakeOutput(tty=True, events=events)
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.sys, "stdout", output), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "show_menu_exit", side_effect=lambda: events.append("exit_feedback")), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
        self.assertLess(events.index("leave_alt"), events.index("exit_feedback"))

    def test_enter_action_restores_normal_mode_then_resumes_navigation(self):
        mode = {"navigation": False}
        events = []

        def setcbreak(_descriptor, _when):
            mode["navigation"] = True
            events.append("navigation")

        def restore(_descriptor, when, _attrs):
            mode["navigation"] = False
            events.append(("restore", when))

        def record(name):
            def inner(*_args, **_kwargs):
                events.append((name, mode["navigation"]))
                return True
            return inner

        stdin = self.FakeTTY("\rq")
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak", side_effect=setcbreak), \
             mock.patch.object(nasberrypi.termios, "tcsetattr", side_effect=restore), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear", side_effect=record("clear")), \
             mock.patch.object(nasberrypi, "show_action_feedback", side_effect=record("feedback")), \
             mock.patch.object(nasberrypi, "protected", side_effect=record("action")), \
             mock.patch.object(nasberrypi, "pause", side_effect=record("pause")), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()

        self.assertIn(("feedback", False), events)
        self.assertIn(("action", False), events)
        self.assertIn(("pause", False), events)
        self.assertEqual(events.count("navigation"), 2)
        self.assertEqual(
            [event for event in events if isinstance(event, tuple) and event[0] == "restore"],
            [("restore", nasberrypi.termios.TCSAFLUSH), ("restore", nasberrypi.termios.TCSAFLUSH)],
        )

    def test_action_transition_leaves_and_reenters_alternate_screen(self):
        events = []

        def setcbreak(_descriptor, _when):
            events.append("navigation")

        def restore(_descriptor, _when, _attrs):
            events.append("restore")

        stdin = self.FakeTTY("\rq")
        output = self.FakeOutput(tty=True, events=events)
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.sys, "stdout", output), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak", side_effect=setcbreak), \
             mock.patch.object(nasberrypi.termios, "tcsetattr", side_effect=restore), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "show_action_feedback", side_effect=lambda *_args: events.append("feedback")), \
             mock.patch.object(nasberrypi, "protected", side_effect=lambda *_args: events.append("action") or True), \
             mock.patch.object(nasberrypi, "pause", side_effect=lambda: events.append("pause")), \
             mock.patch.object(nasberrypi, "show_menu_exit", side_effect=lambda: events.append("exit_feedback")), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
        self.assertEqual(
            events,
            [
                "enter_alt",
                "navigation",
                "restore",
                "leave_alt",
                "feedback",
                "action",
                "pause",
                "enter_alt",
                "navigation",
                "restore",
                "leave_alt",
                "exit_feedback",
            ],
        )

    def test_numeric_shortcut_restores_normal_mode_then_resumes_navigation(self):
        mode = {"navigation": False}
        events = []

        def setcbreak(_descriptor, _when):
            mode["navigation"] = True
            events.append("navigation")

        def restore(_descriptor, when, _attrs):
            mode["navigation"] = False
            events.append(("restore", when))

        def record(name):
            def inner(*_args, **_kwargs):
                events.append((name, mode["navigation"]))
                return True
            return inner

        stdin = self.FakeTTY("6q")
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak", side_effect=setcbreak), \
             mock.patch.object(nasberrypi.termios, "tcsetattr", side_effect=restore), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear", side_effect=record("clear")), \
             mock.patch.object(nasberrypi, "show_action_feedback", side_effect=record("feedback")), \
             mock.patch.object(nasberrypi, "doctor", side_effect=record("action")), \
             mock.patch.object(nasberrypi, "pause", side_effect=record("pause")), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()

        self.assertIn(("feedback", False), events)
        self.assertIn(("action", False), events)
        self.assertIn(("pause", False), events)
        self.assertEqual(events.count("navigation"), 2)
        self.assertEqual(
            [event for event in events if isinstance(event, tuple) and event[0] == "restore"],
            [("restore", nasberrypi.termios.TCSAFLUSH), ("restore", nasberrypi.termios.TCSAFLUSH)],
        )

    def test_enter_action_invalidates_cached_status_before_next_render(self):
        stdin = self.FakeTTY("\rq")
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.time, "monotonic", side_effect=[0.0, 0.1]), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"), \
             mock.patch.object(nasberrypi, "menu_status_lines", side_effect=[["before"], ["after"]]) as status, \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard") as render, \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_action_feedback"), \
             mock.patch.object(nasberrypi, "protected", return_value=True), \
             mock.patch.object(nasberrypi, "pause"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()

        self.assertEqual(status.call_count, 2)
        self.assertEqual(render.call_args_list[0].args[2], ["before"])
        self.assertEqual(render.call_args_list[1].args[2], ["after"])

    def test_numeric_action_invalidates_cached_status_before_next_render(self):
        stdin = self.FakeTTY("6q")
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.time, "monotonic", side_effect=[0.0, 0.1]), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"), \
             mock.patch.object(nasberrypi, "menu_status_lines", side_effect=[["before"], ["after"]]) as status, \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard") as render, \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_action_feedback"), \
             mock.patch.object(nasberrypi, "doctor", return_value=True), \
             mock.patch.object(nasberrypi, "pause"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()

        self.assertEqual(status.call_count, 2)
        self.assertEqual(render.call_args_list[0].args[2], ["before"])
        self.assertEqual(render.call_args_list[1].args[2], ["after"])

    def test_dashboard_blocks_operational_action_when_config_is_missing(self):
        keys = ["1", "q"]
        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "not configured"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "read_menu_key", side_effect=keys), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_action_feedback"), \
             mock.patch.object(nasberrypi, "protected") as protected, \
             mock.patch.object(nasberrypi, "pause"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
        protected.assert_not_called()

    def test_dashboard_allows_diagnostics_when_config_is_invalid(self):
        keys = ["6", "q"]
        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "bad config"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "read_menu_key", side_effect=keys), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_action_feedback"), \
             mock.patch.object(nasberrypi, "doctor", return_value=True) as doctor, \
             mock.patch.object(nasberrypi, "pause"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
        doctor.assert_called_once_with()

    def test_dashboard_setup_selection_reaches_setup_refusal_when_config_is_invalid(self):
        keys = ["7", "q"]
        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "bad config"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "read_menu_key", side_effect=keys), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_action_feedback"), \
             mock.patch.object(nasberrypi, "setup", return_value=False) as setup, \
             mock.patch.object(nasberrypi, "pause"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
        setup.assert_called_once_with()

    def test_keyboard_interrupt_restores_tty_navigation_mode(self):
        stdin = self.FakeTTY("")
        output = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.sys, "stdout", output), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr") as setattrs, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "draw_screen"), \
             mock.patch.object(nasberrypi, "read_menu_key", side_effect=KeyboardInterrupt), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()

        setattrs.assert_called_once_with(7, nasberrypi.termios.TCSAFLUSH, ["original"])
        self.assertEqual(output.writes, ["\033[?1049h", "\033[?1049l"])

    def test_unexpected_exception_restores_tty_navigation_mode(self):
        stdin = self.FakeTTY("")
        output = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.sys, "stdin", stdin), \
             mock.patch.object(nasberrypi.sys, "stdout", output), \
             mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr") as setattrs, \
             mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", return_value="dashboard"), \
             mock.patch.object(nasberrypi, "draw_screen", side_effect=RuntimeError("boom")), \
             mock.patch.object(nasberrypi, "clear"), \
             mock.patch.object(nasberrypi, "show_menu_exit"), \
             mock.patch("builtins.print"), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            with self.assertRaises(RuntimeError):
                nasberrypi.menu()

        setattrs.assert_called_once_with(7, nasberrypi.termios.TCSAFLUSH, ["original"])
        self.assertEqual(output.writes, ["\033[?1049h", "\033[?1049l"])

    def test_read_menu_key_non_tty_uses_prompt(self):
        with mock.patch.object(nasberrypi.sys, "stdin", self.FakeTTY(tty=False)), \
             mock.patch("builtins.input", return_value=" 7 "):
            self.assertEqual(nasberrypi.read_menu_key(), "7")

    def test_dashboard_terminal_enters_and_leaves_alternate_screen_once(self):
        stdin = self.FakeTTY(tty=True)
        output = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"):
            terminal = nasberrypi.DashboardTerminal(input_stream=stdin, output_stream=output)
            terminal.enter_alternate_screen()
            terminal.enter_alternate_screen()
            terminal.leave_alternate_screen()
            terminal.leave_alternate_screen()
        self.assertEqual(output.writes, ["\033[?1049h", "\033[?1049l"])
        self.assertFalse(terminal.alternate_active)

    def test_dashboard_terminal_uses_alternate_screen_only_when_input_and_output_are_ttys(self):
        stdout_pipe = self.FakeOutput(tty=False)
        stdin_tty = self.FakeTTY(tty=True)
        with mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak") as setcbreak, \
             mock.patch.object(nasberrypi.termios, "tcsetattr"):
            terminal = nasberrypi.DashboardTerminal(input_stream=stdin_tty, output_stream=stdout_pipe)
            with terminal:
                pass
        self.assertEqual(stdout_pipe.writes, [])
        setcbreak.assert_called_once_with(7, nasberrypi.termios.TCSANOW)

        stdin_pipe = self.FakeTTY(tty=False)
        stdout_tty = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.termios, "tcgetattr") as getattrs, \
             mock.patch.object(nasberrypi.tty, "setcbreak") as setcbreak:
            terminal = nasberrypi.DashboardTerminal(input_stream=stdin_pipe, output_stream=stdout_tty)
            with terminal:
                pass
        self.assertEqual(stdout_tty.writes, [])
        getattrs.assert_not_called()
        setcbreak.assert_not_called()

    def test_dashboard_terminal_context_restores_states_after_exception(self):
        stdin = self.FakeTTY(tty=True)
        output = self.FakeOutput(tty=True)
        with mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"):
            terminal = nasberrypi.DashboardTerminal(input_stream=stdin, output_stream=output)
            with self.assertRaises(RuntimeError):
                with terminal:
                    raise RuntimeError("boom")
        self.assertFalse(terminal.navigation_active)
        self.assertFalse(terminal.alternate_active)
        self.assertEqual(output.writes, ["\033[?1049h", "\033[?1049l"])

    def test_dashboard_terminal_enter_failure_leaves_alternate_screen(self):
        stdin = self.FakeTTY(tty=True)
        output = self.FakeOutput(tty=True)
        error = OSError("cbreak failed")
        with mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak", side_effect=error), \
             mock.patch.object(nasberrypi.termios, "tcsetattr") as setattrs:
            terminal = nasberrypi.DashboardTerminal(input_stream=stdin, output_stream=output)
            with self.assertRaises(OSError) as raised:
                with terminal:
                    pass
        self.assertIs(raised.exception, error)
        self.assertFalse(terminal.navigation_active)
        self.assertFalse(terminal.alternate_active)
        self.assertEqual(output.writes, ["\033[?1049h", "\033[?1049l"])
        setattrs.assert_not_called()

    def test_dashboard_terminal_alternate_enter_flush_failure_allows_cleanup(self):
        class FlushFailOutput(self.FakeOutput):
            def flush(self):
                if "\033[?1049h" in self.writes and "\033[?1049l" not in self.writes:
                    raise OSError("flush failed")
                super().flush()

        stdin = self.FakeTTY(tty=True)
        output = FlushFailOutput(tty=True)
        with mock.patch.object(nasberrypi.termios, "tcgetattr", return_value=["original"]), \
             mock.patch.object(nasberrypi.tty, "setcbreak"), \
             mock.patch.object(nasberrypi.termios, "tcsetattr"):
            terminal = nasberrypi.DashboardTerminal(input_stream=stdin, output_stream=output)
            with self.assertRaisesRegex(OSError, "flush failed"):
                with terminal:
                    pass
        self.assertFalse(terminal.navigation_active)
        self.assertFalse(terminal.alternate_active)
        self.assertEqual(output.writes, ["\033[?1049h", "\033[?1049l"])

    def test_print_shares_empty_list_does_not_reload_or_invent_public(self):
        with mock.patch.object(nasberrypi, "load_shares") as load_shares, \
             mock.patch("builtins.print") as output:
            nasberrypi.print_shares([])
        load_shares.assert_not_called()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("(none configured)", rendered)
        self.assertNotIn("Public", rendered)

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    @mock.patch.object(nasberrypi, "load_shares", side_effect=nasberrypi.ShareConfigError("invalid shares"))
    @mock.patch.object(nasberrypi, "save_shares")
    def test_manage_shares_refuses_broken_config_without_saving(self, save_shares, _load_shares, _geteuid):
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.manage_shares())
        save_shares.assert_not_called()

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_manage_shares_leaves_malformed_source_file_unchanged(self, _geteuid):
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text("{broken")
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), mock.patch("builtins.print"):
                self.assertFalse(nasberrypi.manage_shares())
            self.assertEqual(shares_file.read_text(), "{broken")

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_manage_shares_removing_final_share_saves_empty_list(self, _geteuid):
        shares = [{"name": "Only", "path": "/mnt/nasberry/Only", "enabled": True, "read_only": False}]
        with mock.patch.object(nasberrypi, "load_shares", return_value=shares), \
             mock.patch.object(nasberrypi, "save_shares") as save_shares, \
             mock.patch("builtins.input", side_effect=["4", "Only", "y", "q"]), \
             mock.patch("builtins.print"):
            self.assertTrue(nasberrypi.manage_shares())
        save_shares.assert_called_once_with([])

    @mock.patch("builtins.print")
    @mock.patch.object(nasberrypi, "show_menu_exit")
    @mock.patch.object(nasberrypi, "draw_screen")
    @mock.patch.object(nasberrypi, "clear")
    def test_repeated_down_navigation_wraps_through_exit(self, _clear, _draw, show_exit, _print):
        selections = []

        def render(_actions, selected=0, _status_lines=None):
            selections.append(selected)
            return "dashboard"

        keys = ["\x1b[b"] * 12 + ["q"]
        with mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.object(nasberrypi, "render_menu", side_effect=render), \
             mock.patch.object(nasberrypi, "read_menu_key", side_effect=keys), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
        self.assertEqual(selections, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1, 2])
        show_exit.assert_called_once_with()

    @mock.patch("builtins.print")
    @mock.patch.object(nasberrypi, "show_menu_exit")
    @mock.patch.object(nasberrypi, "render_menu", return_value="dashboard")
    @mock.patch.object(nasberrypi, "read_menu_key", return_value="q")
    @mock.patch.object(nasberrypi, "clear")
    def test_menu_q_exits_cleanly(self, _clear, _read_key, _render, show_exit, _print):
        with mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
            self.assertFalse(nasberrypi.state["running"])
        show_exit.assert_called_once_with()

    @mock.patch("builtins.print")
    @mock.patch.object(nasberrypi, "show_menu_exit")
    @mock.patch.object(nasberrypi, "render_menu", return_value="dashboard")
    @mock.patch.object(nasberrypi, "read_menu_key", return_value="\x03")
    @mock.patch.object(nasberrypi, "clear")
    def test_menu_ctrl_c_key_exits_cleanly(self, _clear, _read_key, _render, show_exit, _print):
        with mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
            self.assertFalse(nasberrypi.state["running"])
        show_exit.assert_called_once_with()

    @mock.patch("builtins.print")
    @mock.patch.object(nasberrypi, "show_menu_exit")
    @mock.patch.object(nasberrypi, "render_menu", return_value="dashboard")
    @mock.patch.object(nasberrypi, "read_menu_key", side_effect=KeyboardInterrupt)
    @mock.patch.object(nasberrypi, "clear")
    def test_menu_keyboard_interrupt_exits_cleanly(self, _clear, _read_key, _render, show_exit, _print):
        with mock.patch.object(nasberrypi, "menu_status_lines", return_value=self.dashboard_status_lines()), \
             mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
            self.assertFalse(nasberrypi.state["running"])
        show_exit.assert_called_once_with()

    def test_windows_credential_hint_shows_session_reset_command(self):
        with mock.patch("builtins.print") as output:
            nasberrypi.print_windows_credential_hint()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("net use * /delete /y", rendered)
        self.assertIn("Public", rendered)

    def test_usable_address_only_accepts_non_local_ipv4(self):
        self.assertTrue(nasberrypi.usable_address("192.168.1.25"))
        self.assertFalse(nasberrypi.usable_address("127.0.0.1"))
        self.assertFalse(nasberrypi.usable_address("fe80::1"))
        self.assertFalse(nasberrypi.usable_address("not-an-address"))

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    def test_local_addresses_uses_ip_json_output(self, _command_exists, run):
        run.return_value.returncode = 0
        run.return_value.stdout = '[{"addr_info": [{"local": "192.168.1.25"}]}]'
        self.assertEqual(nasberrypi.local_addresses(), ["192.168.1.25"])
        run.assert_called_once_with(["ip", "-json", "-4", "address", "show", "scope", "global"])

    def test_connection_info_suppresses_public_hint_when_zero_enabled(self):
        with mock.patch.object(nasberrypi, "enabled_shares", return_value=[]), \
             mock.patch.object(nasberrypi, "local_addresses") as addresses, \
             mock.patch("builtins.print") as output:
            nasberrypi.print_connection_info()
        addresses.assert_not_called()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("no Nasberry shared folders are enabled", rendered)
        self.assertNotIn("Public", rendered)

    def test_connection_info_preserves_existing_output_when_enabled(self):
        with mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "local_addresses", return_value=["192.168.1.25"]), \
             mock.patch("builtins.print") as output:
            nasberrypi.print_connection_info()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn(r"\\192.168.1.25\Public", rendered)
        self.assertIn("smb://192.168.1.25/Public", rendered)

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    def test_lsblk_devices_excludes_swap_and_zram(self, _command_exists, run):
        run.return_value.returncode = 0
        run.return_value.stdout = """{"blockdevices": [
            {"path": "/dev/sda1", "type": "part", "fstype": "exfat", "mountpoints": [null], "rm": true},
            {"path": "/dev/zram0", "type": "disk", "fstype": "swap", "mountpoints": ["[SWAP]"], "rm": false}
        ]}"""
        self.assertEqual([item["path"] for item in nasberrypi.lsblk_devices()], ["/dev/sda1"])

    def test_repair_samba_missing_linux_user_blocks_storage_side_effects(self):
        with mock.patch.object(nasberrypi.os, "geteuid", return_value=0), \
             mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"), \
             mock.patch.object(nasberrypi.pwd, "getpwnam", side_effect=KeyError), \
             mock.patch.object(nasberrypi, "share_config_preflight") as share_preflight, \
             mock.patch.object(nasberrypi, "samba_config_preflight") as samba_preflight, \
             mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
             mock.patch.object(nasberrypi, "ensure_storage_layout") as ensure_layout, \
             mock.patch.object(nasberrypi, "ensure_share_folders") as ensure_shares, \
             mock.patch.object(nasberrypi, "configure_samba_share") as configure, \
             mock.patch.object(nasberrypi, "restart_samba_service") as restart, \
             mock.patch.object(nasberrypi, "write_state") as write_state, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.repair_samba_share())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Configured Linux user does not exist", rendered)
        self.assertIn("sudo nasberry setup", rendered)
        share_preflight.assert_not_called()
        samba_preflight.assert_not_called()
        mount_storage.assert_not_called()
        ensure_layout.assert_not_called()
        ensure_shares.assert_not_called()
        configure.assert_not_called()
        restart.assert_not_called()
        write_state.assert_not_called()

    @mock.patch.object(nasberrypi, "restart_samba_service")
    @mock.patch.object(nasberrypi, "configure_samba_share")
    @mock.patch.object(nasberrypi, "ensure_share_folders")
    @mock.patch.object(nasberrypi, "ensure_storage_layout")
    @mock.patch.object(nasberrypi, "mount_storage")
    @mock.patch.object(nasberrypi, "samba_config_preflight")
    @mock.patch.object(nasberrypi, "load_shares", side_effect=nasberrypi.ShareConfigError("invalid shares"))
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_preflights_shares_before_side_effects(
        self, _geteuid, _load_shares, samba_preflight, mount_storage, ensure_layout, ensure_shares, configure, restart
    ):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.repair_samba_share())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Share configuration error: invalid shares", rendered)
        samba_preflight.assert_not_called()
        mount_storage.assert_not_called()
        ensure_layout.assert_not_called()
        ensure_shares.assert_not_called()
        configure.assert_not_called()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi, "restart_samba_service")
    @mock.patch.object(nasberrypi, "configure_samba_share")
    @mock.patch.object(nasberrypi, "ensure_share_folders")
    @mock.patch.object(nasberrypi, "ensure_storage_layout")
    @mock.patch.object(nasberrypi, "mount_storage")
    @mock.patch.object(nasberrypi, "samba_config_preflight")
    @mock.patch.object(nasberrypi, "share_config_preflight", return_value=False)
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_config_failure_blocks_external_relocation_path(
        self, _geteuid, _share_preflight, samba_preflight, mount_storage, ensure_layout, ensure_shares, configure, restart
    ):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.repair_samba_share())
        samba_preflight.assert_not_called()
        mount_storage.assert_not_called()
        ensure_layout.assert_not_called()
        ensure_shares.assert_not_called()
        configure.assert_not_called()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi, "restart_samba_service")
    @mock.patch.object(nasberrypi, "configure_samba_share")
    @mock.patch.object(nasberrypi, "ensure_share_folders")
    @mock.patch.object(nasberrypi, "ensure_storage_layout")
    @mock.patch.object(nasberrypi, "mount_storage")
    @mock.patch.object(nasberrypi, "samba_config_preflight")
    @mock.patch.object(nasberrypi, "share_config_preflight", return_value=False)
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_config_failure_blocks_active_samba_remount_path(
        self, _geteuid, _share_preflight, samba_preflight, mount_storage, ensure_layout, ensure_shares, configure, restart
    ):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.repair_samba_share())
        samba_preflight.assert_not_called()
        mount_storage.assert_not_called()
        ensure_layout.assert_not_called()
        ensure_shares.assert_not_called()
        configure.assert_not_called()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "service_active", return_value=True)
    @mock.patch.object(nasberrypi, "service_exists", return_value=True)
    @mock.patch.object(nasberrypi, "run", return_value=mock.Mock(returncode=0, stderr=""))
    def test_restart_samba_service_synchronizes_active_state(self, run, _exists, _active, _mounted, write_state):
        self.assertTrue(nasberrypi.restart_samba_service())
        run.assert_called_once_with(nasberrypi.sudo_cmd("systemctl", "restart", nasberrypi.SAMBA_SERVICE))
        write_state.assert_called_once_with(True, True)

    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "service_exists", return_value=True)
    @mock.patch.object(nasberrypi, "run", return_value=mock.Mock(returncode=1, stderr="failed"))
    def test_restart_samba_service_synchronizes_failed_state(self, _run, _exists, _active, _mounted, write_state):
        with mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.restart_samba_service())
        write_state.assert_called_once_with(True, False)

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_mounts_repairs_and_restarts(self, _geteuid):
        calls = []

        def step(name):
            def _inner(*_args, **_kwargs):
                calls.append(name)
                return True
            return _inner

        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "share_user_preflight", side_effect=step("user_preflight")) as user_preflight, \
             mock.patch.object(nasberrypi, "share_config_preflight", side_effect=step("share_preflight")) as share_preflight, \
             mock.patch.object(nasberrypi, "samba_config_preflight", side_effect=step("samba_preflight")) as samba_preflight, \
             mock.patch.object(nasberrypi, "mount_storage", side_effect=step("mount")) as mount_storage, \
             mock.patch.object(nasberrypi, "ensure_storage_layout", side_effect=step("layout")) as ensure_layout, \
             mock.patch.object(nasberrypi, "ensure_share_folders", side_effect=step("folders")) as ensure_shares, \
             mock.patch.object(nasberrypi, "configure_samba_share", side_effect=step("configure")) as configure, \
             mock.patch.object(nasberrypi, "restart_samba_service", side_effect=step("restart")) as restart:
            self.assertTrue(nasberrypi.repair_samba_share())
        self.assertEqual(calls, ["user_preflight", "share_preflight", "samba_preflight", "mount", "layout", "folders", "configure", "restart"])
        user_preflight.assert_called_once_with("kali")
        share_preflight.assert_called_once_with()
        samba_preflight.assert_called_once_with()
        mount_storage.assert_called_once_with(repair_permissions=True, confirm_external_move=True)
        ensure_layout.assert_called_once_with()
        ensure_shares.assert_called_once_with()
        configure.assert_called_once_with()
        restart.assert_called_once_with()

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "restart_samba_service")
    @mock.patch.object(nasberrypi, "configure_samba_share")
    @mock.patch.object(nasberrypi, "ensure_share_folders", return_value=False)
    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True)
    @mock.patch.object(nasberrypi, "mount_storage", return_value=True)
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_fails_cleanly_when_share_folders_fail(
        self, _geteuid, _mount_storage, _ensure_layout, _preflight, _ensure_shares, configure, restart, _active
    ):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
             mock.patch.object(nasberrypi, "share_config_preflight", return_value=True), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.repair_samba_share())
        configure.assert_not_called()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "restart_samba_service")
    @mock.patch.object(nasberrypi, "configure_samba_share", return_value=False)
    @mock.patch.object(nasberrypi, "ensure_share_folders", return_value=True)
    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True)
    @mock.patch.object(nasberrypi, "mount_storage", return_value=True)
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_leaves_sharing_offline_when_configure_fails(
        self, _geteuid, _mount_storage, _ensure_layout, _preflight, _ensure_shares, configure, restart, _active
    ):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
             mock.patch.object(nasberrypi, "share_config_preflight", return_value=True), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.repair_samba_share())
        configure.assert_called_once_with()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi, "restart_samba_service")
    @mock.patch.object(nasberrypi, "configure_samba_share")
    @mock.patch.object(nasberrypi, "ensure_share_folders")
    @mock.patch.object(nasberrypi, "ensure_storage_layout")
    @mock.patch.object(nasberrypi, "mount_storage", return_value=False)
    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_declined_external_move_stops_later_work(
        self, _geteuid, _preflight, mount_storage, ensure_layout, ensure_shares, configure, restart
    ):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
             mock.patch.object(nasberrypi, "share_config_preflight", return_value=True), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.repair_samba_share())
        mount_storage.assert_called_once_with(repair_permissions=True, confirm_external_move=True)
        ensure_layout.assert_not_called()
        ensure_shares.assert_not_called()
        configure.assert_not_called()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi, "service_active", return_value=False)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "device_mount_points", return_value=[])
    @mock.patch.object(nasberrypi, "is_mounted", return_value=False)
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=[])
    @mock.patch.object(nasberrypi, "time")
    @mock.patch.object(nasberrypi, "run")
    def test_repair_mount_after_external_move_approval_continues_to_mount_point(
        self, run, _time, _options, _nas_mount, _device, _mounted, _mounts, _ensure, _write_state, _service_active
    ):
        run.return_value.returncode = 0
        with mock.patch.object(nasberrypi.sys, "stdin", self.FakeTTY("y\n", tty=True)), \
             mock.patch.object(nasberrypi, "device_mount_points", side_effect=[["/media/foo"], [], [nasberrypi.MOUNT_POINT]]):
            self.assertTrue(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        self.assertEqual(run.call_args_list[0].args[0], nasberrypi.sudo_cmd("umount", "/media/foo"))
        self.assertEqual(run.call_args_list[1].args[0], nasberrypi.sudo_cmd("mount", nasberrypi.DEVICE, nasberrypi.MOUNT_POINT))

    @mock.patch.object(nasberrypi, "service_active", return_value=True)
    @mock.patch.object(nasberrypi, "stop_share", return_value=True)
    @mock.patch.object(nasberrypi, "write_state")
    @mock.patch.object(nasberrypi, "ensure_mount_point", return_value=True)
    @mock.patch.object(nasberrypi, "cleanup_other_mounts", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "is_mounted", side_effect=[True, True, False])
    @mock.patch.object(nasberrypi, "storage_mount_options", return_value=["-o", "uid=1000,gid=1000,umask=0002"])
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    @mock.patch.object(nasberrypi, "time")
    @mock.patch.object(nasberrypi, "run")
    def test_repair_permission_remount_at_nas_mount_remains_automatic(
        self, run, _time, _device, _options, _mounted, _nas_mount, cleanup, _ensure, _write_state, stop_share, _active
    ):
        run.return_value.returncode = 0
        with mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True):
            self.assertTrue(nasberrypi.mount_storage(repair_permissions=True, confirm_external_move=True))
        cleanup.assert_called_once_with(confirm=True)
        stop_share.assert_called_once_with()
        self.assertEqual(run.call_args_list[0].args[0], nasberrypi.sudo_cmd("umount", nasberrypi.MOUNT_POINT))
        self.assertEqual(
            run.call_args_list[1].args[0],
            nasberrypi.sudo_cmd("mount", "-o", "uid=1000,gid=1000,umask=0002", nasberrypi.DEVICE, nasberrypi.MOUNT_POINT),
        )

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "samba_config_valid")
    @mock.patch.object(nasberrypi, "ensure_public_folder", return_value=False)
    @mock.patch.object(nasberrypi, "mount_storage", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True)
    @mock.patch.object(nasberrypi, "service_exists", return_value=True)
    def test_start_share_refuses_when_share_folders_are_not_safe(
        self, _service, _nas_mount, _mount_storage, _ensure_public, samba_valid, run
    ):
        with mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "share_user_preflight", return_value=True), \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.start_share())
        samba_valid.assert_not_called()
        run.assert_not_called()

    def test_start_share_missing_linux_user_blocks_mount_and_state_write(self):
        with mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"), \
             mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "service_exists", return_value=True), \
             mock.patch.object(nasberrypi.pwd, "getpwnam", side_effect=KeyError), \
             mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
             mock.patch.object(nasberrypi, "ensure_public_folder") as ensure_public, \
             mock.patch.object(nasberrypi, "run") as run, \
             mock.patch.object(nasberrypi, "write_state") as write_state, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.start_share())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Configured Linux user does not exist", rendered)
        self.assertIn("sudo nasberry setup", rendered)
        mount_storage.assert_not_called()
        ensure_public.assert_not_called()
        run.assert_not_called()
        write_state.assert_not_called()

    def test_start_share_missing_linux_user_blocks_already_mounted_path(self):
        with mock.patch.object(nasberrypi, "SHARE_USER", "deleteduser"), \
             mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "service_exists", return_value=True), \
             mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True), \
             mock.patch.object(nasberrypi.pwd, "getpwnam", side_effect=KeyError), \
             mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
             mock.patch.object(nasberrypi, "ensure_public_folder") as ensure_public, \
             mock.patch.object(nasberrypi, "run") as run, \
             mock.patch.object(nasberrypi, "write_state") as write_state, \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.start_share())
        mount_storage.assert_not_called()
        ensure_public.assert_not_called()
        run.assert_not_called()
        write_state.assert_not_called()

    def test_start_share_existing_linux_user_allows_normal_path(self):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"), \
             mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "service_exists", return_value=True), \
             mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=True), \
             mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=mock.Mock(pw_uid=1000, pw_gid=1000)), \
             mock.patch.object(nasberrypi, "ensure_public_folder", return_value=True) as ensure_public, \
             mock.patch.object(nasberrypi, "samba_config_valid", return_value=(False, "not ready")), \
             mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
             mock.patch.object(nasberrypi, "run") as run, \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.start_share())
        mount_storage.assert_not_called()
        ensure_public.assert_called_once_with()
        run.assert_not_called()

    def test_status_reports_missing_config_without_using_defaults(self):
        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "not configured"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "device_exists") as device_exists, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.status())
        device_exists.assert_not_called()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Configuration : missing", rendered)
        self.assertNotIn(nasberrypi.DEFAULTS["device"], rendered)

    def test_status_zero_enabled_does_not_claim_sharing_online(self):
        with mock.patch.object(nasberrypi, "enabled_shares", return_value=[]), \
             mock.patch.object(nasberrypi, "service_active", return_value=True) as service_active, \
             mock.patch.object(nasberrypi, "device_exists", return_value=True), \
             mock.patch.object(nasberrypi, "active_mount_point", return_value=nasberrypi.MOUNT_POINT), \
             mock.patch.object(nasberrypi, "storage_mount_state_label", return_value="mounted in NAS mode"), \
             mock.patch.object(nasberrypi, "disk_usage", return_value="10 GB free"), \
             mock.patch.object(nasberrypi, "print_connection_info") as connection_info, \
             mock.patch("builtins.print") as output:
            nasberrypi.status()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("File sharing   : no shares enabled", rendered)
        self.assertIn("Enabled shares : 0", rendered)
        self.assertNotIn("sharing online", rendered)
        service_active.assert_not_called()
        connection_info.assert_not_called()

    def test_status_enabled_active_preserves_online_output(self):
        with mock.patch.object(nasberrypi, "enabled_shares", return_value=[nasberrypi.default_share()]), \
             mock.patch.object(nasberrypi, "service_active", return_value=True), \
             mock.patch.object(nasberrypi, "device_exists", return_value=True), \
             mock.patch.object(nasberrypi, "active_mount_point", return_value=nasberrypi.MOUNT_POINT), \
             mock.patch.object(nasberrypi, "storage_mount_state_label", return_value="mounted in NAS mode"), \
             mock.patch.object(nasberrypi, "disk_usage", return_value="10 GB free"), \
             mock.patch.object(nasberrypi, "print_connection_info") as connection_info, \
             mock.patch("builtins.print") as output:
            nasberrypi.status()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("File sharing   : sharing online", rendered)
        self.assertIn("Enabled shares : 1", rendered)
        connection_info.assert_called_once_with()

    def test_storage_info_reports_invalid_config_without_using_defaults(self):
        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "invalid safe_mode_on_start"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "active_mount_point") as active_mount, \
             mock.patch("builtins.print") as output:
            self.assertFalse(nasberrypi.storage_info())
        active_mount.assert_not_called()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Configuration       : invalid", rendered)
        self.assertIn("invalid safe_mode_on_start", rendered)

    def test_main_version_works_with_invalid_config(self):
        with mock.patch.object(sys, "argv", ["nasberry", "--version"]), \
             mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "bad config"), \
             mock.patch.object(nasberrypi, "enforce_boot_safety") as enforce:
            with self.assertRaises(SystemExit) as exit_context:
                nasberrypi.main()
        self.assertEqual(exit_context.exception.code, 0)
        enforce.assert_not_called()

    def test_main_refuses_operational_commands_without_valid_config(self):
        with mock.patch.object(sys, "argv", ["nasberry", "mount"]), \
             mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "bad config"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.main())
        mount_storage.assert_not_called()

    def test_main_does_not_enforce_safe_mode_from_invalid_config(self):
        with mock.patch.object(sys, "argv", ["nasberry", "doctor"]), \
             mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "bad config"), \
             mock.patch.object(nasberrypi, "SAFE_MODE_ON_START", True), \
             mock.patch.object(nasberrypi, "enforce_boot_safety") as enforce, \
             mock.patch.object(nasberrypi, "doctor", return_value=True):
            self.assertTrue(nasberrypi.main())
        enforce.assert_not_called()

    def test_safe_mode_command_refuses_invalid_config(self):
        with mock.patch.object(sys, "argv", ["nasberry", "safe-mode", "--yes"]), \
             mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "bad config"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "enforce_boot_safety") as enforce, \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.main())
        enforce.assert_not_called()

    def test_main_enforces_safe_mode_when_valid_config_requests_it(self):
        with mock.patch.object(sys, "argv", ["nasberry", "doctor"]), \
             mock.patch.object(nasberrypi, "SAFE_MODE_ON_START", True), \
             mock.patch.object(nasberrypi, "enforce_boot_safety", return_value=True) as enforce, \
             mock.patch.object(nasberrypi, "doctor", return_value=True):
            self.assertTrue(nasberrypi.main())
        enforce.assert_called_once_with()

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_refuses_invalid_config_without_overwriting(self, _geteuid):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text("[broken")
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file), \
                 mock.patch.object(nasberrypi, "CONFIG_STATUS", "invalid"), \
                 mock.patch.object(nasberrypi, "CONFIG_ERROR", "invalid configuration"), \
                 mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
                 mock.patch.object(nasberrypi, "choose_device") as choose_device, \
                 mock.patch.object(nasberrypi, "save_config") as save_config, \
                 mock.patch("builtins.print"):
                self.assertFalse(nasberrypi.setup())
            self.assertEqual(config_file.read_text(), "[broken")
        choose_device.assert_not_called()
        save_config.assert_not_called()

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_is_allowed_when_config_is_missing(self, _geteuid):
        with mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
             mock.patch.object(nasberrypi, "CONFIG_ERROR", "not configured"), \
             mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
             mock.patch.object(nasberrypi, "choose_device", return_value=None) as choose_device, \
             mock.patch("builtins.print"):
            self.assertFalse(nasberrypi.setup())
        choose_device.assert_called_once_with(False)

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_refuses_corrupt_existing_shares_before_mutating_config_or_storage(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        settings = config["nasberry"]
        settings["device"] = "/dev/old"
        settings["mount_point"] = "/mnt/old"
        settings["share_name"] = "Old"
        settings["share_user"] = "olduser"
        settings["pin_hash"] = "old-pin"
        original_settings = dict(settings)
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text("{broken")
            with mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi, "config", config), \
                 mock.patch.object(nasberrypi, "settings", settings), \
                 mock.patch.object(nasberrypi, "CONFIG_STATUS", "valid"), \
                 mock.patch.object(nasberrypi, "CONFIG_ERROR", None), \
                 mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
                 mock.patch.object(nasberrypi, "choose_device", return_value=selected), \
                 mock.patch.object(nasberrypi, "setup_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "save_config") as save_config, \
                 mock.patch.object(nasberrypi, "publish_config_from_disk") as publish, \
                 mock.patch.object(nasberrypi, "ensure_default_shares_file") as ensure_default, \
                 mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
                 mock.patch.object(nasberrypi, "ensure_storage_layout") as ensure_storage_layout, \
                 mock.patch.object(nasberrypi, "ensure_share_folders") as ensure_share_folders, \
                 mock.patch.object(nasberrypi, "configure_samba_share") as configure_samba, \
                 mock.patch.object(nasberrypi, "restart_samba_service") as restart, \
                 mock.patch.object(nasberrypi.getpass, "getpass") as pin_prompt, \
                 mock.patch("builtins.input", return_value="sparkles"), \
                 mock.patch("builtins.print") as output:
                self.assertFalse(nasberrypi.setup(non_interactive=False, skip_pin=False))
            rendered = "\n".join(call.args[0] for call in output.call_args_list)
            self.assertIn("Share configuration error", rendered)
            self.assertIn("will not overwrite", rendered)
            self.assertEqual(shares_file.read_text(), "{broken")
            self.assertEqual(dict(settings), original_settings)
        pin_prompt.assert_not_called()
        save_config.assert_not_called()
        publish.assert_not_called()
        ensure_default.assert_not_called()
        mount_storage.assert_not_called()
        ensure_storage_layout.assert_not_called()
        ensure_share_folders.assert_not_called()
        configure_samba.assert_not_called()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_refuses_missing_shares_bootstrap_failure_before_mutation(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        settings = config["nasberry"]
        settings["device"] = "/dev/old"
        settings["mount_point"] = "/mnt/old"
        settings["share_name"] = "Old"
        settings["share_user"] = "olduser"
        settings["pin_hash"] = "old-pin"
        original_settings = dict(settings)
        with tempfile.TemporaryDirectory() as directory:
            shares_file = Path(directory) / "shares.json"
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(nasberrypi, "SHARES_FILE", shares_file))
                stack.enter_context(mock.patch.object(nasberrypi, "config", config))
                stack.enter_context(mock.patch.object(nasberrypi, "settings", settings))
                stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_STATUS", "valid"))
                stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_ERROR", None))
                stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None))
                stack.enter_context(mock.patch.object(nasberrypi, "choose_device", return_value=selected))
                stack.enter_context(mock.patch.object(nasberrypi, "setup_preflight", return_value=True))
                stack.enter_context(mock.patch.object(nasberrypi, "setup_share_config_preflight", return_value=True))
                stack.enter_context(mock.patch.object(nasberrypi, "ensure_default_shares_file", return_value=False))
                share_preflight = stack.enter_context(mock.patch.object(nasberrypi, "share_config_preflight"))
                save_config = stack.enter_context(mock.patch.object(nasberrypi, "save_config"))
                publish = stack.enter_context(mock.patch.object(nasberrypi, "publish_config_from_disk"))
                mount_storage = stack.enter_context(mock.patch.object(nasberrypi, "mount_storage"))
                ensure_storage_layout = stack.enter_context(mock.patch.object(nasberrypi, "ensure_storage_layout"))
                ensure_share_folders = stack.enter_context(mock.patch.object(nasberrypi, "ensure_share_folders"))
                configure_samba = stack.enter_context(mock.patch.object(nasberrypi, "configure_samba_share"))
                restart = stack.enter_context(mock.patch.object(nasberrypi, "restart_samba_service"))
                pin_prompt = stack.enter_context(mock.patch.object(nasberrypi.getpass, "getpass"))
                stack.enter_context(mock.patch("builtins.input", return_value="sparkles"))
                stack.enter_context(mock.patch("builtins.print"))
                self.assertFalse(nasberrypi.setup(non_interactive=False, skip_pin=False))
            self.assertEqual(dict(settings), original_settings)
            self.assertFalse(shares_file.exists())
        pin_prompt.assert_not_called()
        share_preflight.assert_not_called()
        save_config.assert_not_called()
        publish.assert_not_called()
        mount_storage.assert_not_called()
        ensure_storage_layout.assert_not_called()
        ensure_share_folders.assert_not_called()
        configure_samba.assert_not_called()
        restart.assert_not_called()

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_successful_first_time_setup_publishes_valid_config_state(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file), \
                 mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi, "config", config), \
                 mock.patch.object(nasberrypi, "settings", config["nasberry"]), \
                 mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
                 mock.patch.object(nasberrypi, "CONFIG_ERROR", f"not configured: {config_file}"), \
                 mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
                 mock.patch.object(nasberrypi, "MOUNT_POINT", "/srv/nasberry-test"), \
                 mock.patch.object(nasberrypi, "choose_device", return_value=selected), \
                 mock.patch.object(nasberrypi, "setup_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "mount_storage", return_value=True) as mount_storage, \
                 mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True), \
                 mock.patch.object(nasberrypi, "ensure_share_folders", return_value=True), \
                 mock.patch.object(nasberrypi, "configure_samba_share", return_value=True), \
                 mock.patch.object(nasberrypi, "restart_samba_service", return_value=True), \
                 mock.patch("builtins.print"):
                self.assertTrue(nasberrypi.setup(non_interactive=True, skip_pin=True, share_user_arg="sparkles"))
                self.assertEqual(nasberrypi.CONFIG_STATUS, "valid")
                self.assertIsNone(nasberrypi.CONFIG_ERROR)
                self.assertTrue(nasberrypi.config_ready())
                self.assertEqual(nasberrypi.settings["device"], "/dev/disk/by-uuid/abc-123")
                self.assertEqual(nasberrypi.DEVICE, "/dev/disk/by-uuid/abc-123")
                self.assertEqual(nasberrypi.settings["mount_point"], "/srv/nasberry-test")
                self.assertEqual(nasberrypi.MOUNT_POINT, "/srv/nasberry-test")
                self.assertEqual(nasberrypi.settings["share_user"], "sparkles")
                self.assertIn("Public", shares_file.read_text())
        mount_storage.assert_called_once_with(repair_permissions=True, confirm_external_move=False)

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_preserves_existing_valid_custom_shares(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        custom = '{\n  "shares": [{"name": "Media", "path": "Media", "read_only": true}]\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            shares_file = Path(directory) / "shares.json"
            shares_file.write_text(custom)
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file), \
                 mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi, "config", config), \
                 mock.patch.object(nasberrypi, "settings", config["nasberry"]), \
                 mock.patch.object(nasberrypi, "CONFIG_STATUS", "valid"), \
                 mock.patch.object(nasberrypi, "CONFIG_ERROR", None), \
                 mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
                 mock.patch.object(nasberrypi, "MOUNT_POINT", "/srv/nasberry-test"), \
                 mock.patch.object(nasberrypi, "choose_device", return_value=selected), \
                 mock.patch.object(nasberrypi, "setup_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "mount_storage", return_value=True) as mount_storage, \
                 mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True), \
                 mock.patch.object(nasberrypi, "ensure_share_folders", return_value=True), \
                 mock.patch.object(nasberrypi, "configure_samba_share", return_value=True), \
                 mock.patch.object(nasberrypi, "restart_samba_service", return_value=True), \
                 mock.patch("builtins.print"):
                self.assertTrue(nasberrypi.setup(non_interactive=True, skip_pin=True, share_user_arg="sparkles"))
            self.assertEqual(shares_file.read_text(), custom)
        mount_storage.assert_called_once_with(repair_permissions=True, confirm_external_move=False)

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_successful_interactive_setup_suppresses_public_hint_when_zero_enabled(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        password_result = mock.Mock(returncode=0)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(nasberrypi, "config", config))
            stack.enter_context(mock.patch.object(nasberrypi, "settings", config["nasberry"]))
            stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_STATUS", "valid"))
            stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_ERROR", None))
            stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None))
            stack.enter_context(mock.patch.object(nasberrypi, "choose_device", return_value=selected))
            stack.enter_context(mock.patch.object(nasberrypi, "setup_preflight", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "setup_share_config_preflight", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "ensure_default_shares_file", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "share_config_preflight", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "save_config"))
            stack.enter_context(mock.patch.object(nasberrypi, "publish_config_from_disk", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "mount_storage", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "ensure_share_folders", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "configure_samba_share", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "command_exists", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi.subprocess, "run", return_value=password_result))
            stack.enter_context(mock.patch.object(nasberrypi, "samba_account_valid", return_value=(True, "enabled")))
            stack.enter_context(mock.patch.object(nasberrypi, "restart_samba_service", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "enabled_shares", return_value=[]))
            hint = stack.enter_context(mock.patch.object(nasberrypi, "print_windows_credential_hint"))
            stack.enter_context(mock.patch("builtins.input", return_value="sparkles"))
            stack.enter_context(mock.patch.object(nasberrypi.getpass, "getpass", side_effect=["1234", "1234"]))
            stack.enter_context(mock.patch("builtins.print"))
            self.assertTrue(nasberrypi.setup(non_interactive=False, skip_pin=False))
        hint.assert_not_called()

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_successful_setup_does_not_raise_when_connection_hint_share_check_fails(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        password_result = mock.Mock(returncode=0)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(nasberrypi, "config", config))
            stack.enter_context(mock.patch.object(nasberrypi, "settings", config["nasberry"]))
            stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_STATUS", "valid"))
            stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_ERROR", None))
            stack.enter_context(mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None))
            stack.enter_context(mock.patch.object(nasberrypi, "choose_device", return_value=selected))
            stack.enter_context(mock.patch.object(nasberrypi, "setup_preflight", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "setup_share_config_preflight", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "ensure_default_shares_file", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "share_config_preflight", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "save_config"))
            stack.enter_context(mock.patch.object(nasberrypi, "publish_config_from_disk", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "mount_storage", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "ensure_share_folders", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "configure_samba_share", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "command_exists", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi.subprocess, "run", return_value=password_result))
            stack.enter_context(mock.patch.object(nasberrypi, "samba_account_valid", return_value=(True, "enabled")))
            stack.enter_context(mock.patch.object(nasberrypi, "restart_samba_service", return_value=True))
            stack.enter_context(mock.patch.object(nasberrypi, "enabled_shares", side_effect=nasberrypi.ShareConfigError("changed after setup")))
            hint = stack.enter_context(mock.patch.object(nasberrypi, "print_windows_credential_hint"))
            stack.enter_context(mock.patch("builtins.input", return_value="sparkles"))
            stack.enter_context(mock.patch.object(nasberrypi.getpass, "getpass", side_effect=["1234", "1234"]))
            output = stack.enter_context(mock.patch("builtins.print"))
            self.assertTrue(nasberrypi.setup(non_interactive=False, skip_pin=False))
        hint.assert_not_called()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Connection help unavailable", rendered)
        self.assertIn("changed after setup", rendered)

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_bootstraps_missing_shares_then_validates_before_mounting(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        events = []

        def setup_shares_preflight():
            events.append("setup_shares_preflight")
            return True

        def bootstrap():
            events.append("bootstrap")
            return True

        def validate():
            events.append("validate")
            return True

        def save():
            events.append("save")

        def publish():
            events.append("publish")
            return True

        def mount(*_args, **_kwargs):
            events.append("mount")
            return True

        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file), \
                 mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi, "config", config), \
                 mock.patch.object(nasberrypi, "settings", config["nasberry"]), \
                 mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
                 mock.patch.object(nasberrypi, "CONFIG_ERROR", f"not configured: {config_file}"), \
                 mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
                 mock.patch.object(nasberrypi, "choose_device", return_value=selected), \
                 mock.patch.object(nasberrypi, "setup_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "setup_share_config_preflight", side_effect=setup_shares_preflight), \
                 mock.patch.object(nasberrypi, "ensure_default_shares_file", side_effect=bootstrap), \
                 mock.patch.object(nasberrypi, "share_config_preflight", side_effect=validate), \
                 mock.patch.object(nasberrypi, "save_config", side_effect=save), \
                 mock.patch.object(nasberrypi, "publish_config_from_disk", side_effect=publish), \
                 mock.patch.object(nasberrypi, "mount_storage", side_effect=mount), \
                 mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True), \
                 mock.patch.object(nasberrypi, "ensure_share_folders", return_value=True), \
                 mock.patch.object(nasberrypi, "configure_samba_share", return_value=True), \
                 mock.patch.object(nasberrypi, "restart_samba_service", return_value=True), \
                 mock.patch("builtins.print"):
                self.assertTrue(nasberrypi.setup(non_interactive=True, skip_pin=True, share_user_arg="sparkles"))
        self.assertEqual(events, ["setup_shares_preflight", "bootstrap", "validate", "save", "publish", "validate", "mount"])

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_stops_before_mount_when_second_share_validation_fails(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()

        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            shares_file = Path(directory) / "shares.json"
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file), \
                 mock.patch.object(nasberrypi, "SHARES_FILE", shares_file), \
                 mock.patch.object(nasberrypi, "config", config), \
                 mock.patch.object(nasberrypi, "settings", config["nasberry"]), \
                 mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
                 mock.patch.object(nasberrypi, "CONFIG_ERROR", f"not configured: {config_file}"), \
                 mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
                 mock.patch.object(nasberrypi, "choose_device", return_value=selected), \
                 mock.patch.object(nasberrypi, "setup_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "setup_share_config_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "ensure_default_shares_file", return_value=True), \
                 mock.patch.object(nasberrypi, "share_config_preflight", side_effect=(True, False)), \
                 mock.patch.object(nasberrypi, "save_config") as save_config, \
                 mock.patch.object(nasberrypi, "publish_config_from_disk", return_value=True) as publish, \
                 mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
                 mock.patch.object(nasberrypi, "ensure_storage_layout") as ensure_storage_layout, \
                 mock.patch.object(nasberrypi, "ensure_share_folders") as ensure_share_folders, \
                 mock.patch.object(nasberrypi, "configure_samba_share") as configure_samba, \
                 mock.patch("builtins.print"):
                self.assertFalse(nasberrypi.setup(non_interactive=True, skip_pin=True, share_user_arg="sparkles"))
        save_config.assert_called_once_with()
        publish.assert_called_once_with()
        mount_storage.assert_not_called()
        ensure_storage_layout.assert_not_called()
        ensure_share_folders.assert_not_called()
        configure_samba.assert_not_called()

    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_setup_stops_if_saved_config_cannot_be_reloaded(self, _geteuid):
        selected = {"path": "/dev/sdz1", "uuid": "abc-123", "fstype": "ext4"}
        config = nasberrypi.default_config()
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            reloaded = nasberrypi.default_config()
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file), \
                 mock.patch.object(nasberrypi, "config", config), \
                 mock.patch.object(nasberrypi, "settings", config["nasberry"]), \
                 mock.patch.object(nasberrypi, "CONFIG_STATUS", "missing"), \
                 mock.patch.object(nasberrypi, "CONFIG_ERROR", f"not configured: {config_file}"), \
                 mock.patch.object(nasberrypi, "CONFIG_RUNTIME_ERROR", None), \
                 mock.patch.object(nasberrypi, "choose_device", return_value=selected), \
                 mock.patch.object(nasberrypi, "setup_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "setup_share_config_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "share_config_preflight", return_value=True), \
                 mock.patch.object(nasberrypi, "load_config_with_status", return_value=(reloaded, "invalid", "invalid after save")), \
                 mock.patch.object(nasberrypi, "ensure_default_shares_file", return_value=True) as ensure_default_shares, \
                 mock.patch.object(nasberrypi, "mount_storage") as mount_storage, \
                 mock.patch.object(nasberrypi, "ensure_storage_layout") as ensure_storage_layout, \
                 mock.patch.object(nasberrypi, "ensure_share_folders") as ensure_share_folders, \
                 mock.patch.object(nasberrypi, "configure_samba_share") as configure_samba, \
                 mock.patch("builtins.print"):
                self.assertFalse(nasberrypi.setup(non_interactive=True, skip_pin=True, share_user_arg="sparkles"))
                self.assertEqual(nasberrypi.CONFIG_STATUS, "invalid")
                self.assertEqual(nasberrypi.CONFIG_ERROR, "invalid after save")
        ensure_default_shares.assert_called_once_with()
        mount_storage.assert_not_called()
        ensure_storage_layout.assert_not_called()
        ensure_share_folders.assert_not_called()
        configure_samba.assert_not_called()

    @mock.patch.object(nasberrypi, "setup", return_value=True)
    def test_cli_setup_accepts_non_interactive_share_user(self, setup):
        arguments = ["nasberry", "setup", "--non-interactive", "--skip-pin", "--share-user", "kali"]
        with mock.patch.object(sys, "argv", arguments):
            self.assertTrue(nasberrypi.main())
        setup.assert_called_once_with(True, True, "kali")


if __name__ == "__main__":
    unittest.main()
