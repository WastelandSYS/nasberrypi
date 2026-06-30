import importlib.util
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
    def test_load_config_ignores_malformed_file_and_preserves_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text("[broken")
            loaded = nasberrypi.load_config(config_file)
        self.assertEqual(loaded["nasberry"]["mount_point"], "/mnt/nasberry")

    def test_save_config_is_private_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            with mock.patch.object(nasberrypi, "CONFIG_FILE", config_file):
                nasberrypi.save_config()
            self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob(".config.ini.*")), [])

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
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            config = nasberrypi.appliance_samba_config()
        self.assertIn("[Public]", config)
        self.assertIn(f"path = {nasberrypi.MOUNT_POINT}/Public", config)
        self.assertIn("valid users = kali", config)
        self.assertIn("force user = kali", config)
        self.assertNotIn("[homes]", config)
        self.assertNotIn("[Nasberry]", config)
        self.assertNotIn("[Private]", config)
        self.assertNotIn("[Backups]", config)

    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_accepts_public_only(self, samba_shares):
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
    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_accepts_multiple_enabled_shares(self, samba_shares, load_shares):
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

    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_rejects_mount_root(self, samba_shares):
        samba_shares.return_value = {"Public": {"path": nasberrypi.MOUNT_POINT, "available": "yes"}}
        valid, reason = nasberrypi.samba_config_valid()
        self.assertFalse(valid)
        self.assertIn(nasberrypi.public_share_path(), reason)

    @mock.patch.object(nasberrypi, "samba_shares")
    def test_samba_config_ignores_unrelated_active_share(self, samba_shares):
        samba_shares.return_value = {
            "homes": {"available": "yes"},
            "Public": {"path": nasberrypi.public_share_path(), "available": "yes"},
        }
        valid, reason = nasberrypi.samba_config_valid()
        self.assertTrue(valid)
        self.assertIn("enabled share", reason)

    @mock.patch.object(nasberrypi.pwd, "getpwnam")
    @mock.patch.object(nasberrypi, "lsblk_devices")
    def test_exfat_mount_options_make_share_user_owner(self, lsblk_devices, getpwnam):
        lsblk_devices.return_value = [{"path": "/dev/sda1", "fstype": "exfat"}]
        getpwnam.return_value = mock.Mock(pw_uid=1000, pw_gid=1000)
        with mock.patch.object(nasberrypi, "DEVICE", "/dev/sda1"), mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            self.assertEqual(nasberrypi.storage_mount_options(), ["-o", "uid=1000,gid=1000,umask=0002"])

    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "samba_config_valid", return_value=(True, "/mnt/nasberry/Public"))
    @mock.patch.object(nasberrypi, "run")
    def test_configure_samba_validates_candidate_before_replacing_live_config(self, run, _valid, _preflight):
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text("original config")
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
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

    @mock.patch.object(nasberrypi, "mount_storage")
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    @mock.patch.object(nasberrypi, "device_mounted_at_nas", return_value=False)
    @mock.patch.object(nasberrypi, "service_exists", return_value=True)
    def test_start_share_refuses_busy_mount_point_with_wrong_device(self, _service, _nas_mount, _mounted, mount_storage):
        mount_storage.return_value = False
        self.assertFalse(nasberrypi.start_share())
        mount_storage.assert_called_once_with()

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
        self.assertFalse(nasberrypi.mount_storage(repair_permissions=True))
        run.assert_not_called()

    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "run")
    def test_configure_samba_keeps_live_config_when_candidate_is_invalid(self, run, _preflight):
        run.return_value.returncode = 1
        run.return_value.stderr = "invalid"
        run.return_value.stdout = ""
        with tempfile.TemporaryDirectory() as directory:
            smb_file = Path(directory) / "smb.conf"
            smb_file.write_text("original config")
            with mock.patch.object(nasberrypi, "Path", side_effect=lambda value: smb_file if value == "/etc/samba/smb.conf" else Path(value)), mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
                self.assertFalse(nasberrypi.configure_samba_share())
            self.assertEqual(smb_file.read_text(), "original config")

    @mock.patch.object(nasberrypi, "samba_config_preflight")
    def test_configure_samba_rejects_unsafe_user_before_preflight(self, preflight):
        with mock.patch.object(nasberrypi, "SHARE_USER", "user\nadmin users = root"):
            self.assertFalse(nasberrypi.configure_samba_share())
        preflight.assert_not_called()

    @mock.patch.object(nasberrypi, "filesystem_uses_mount_permissions", return_value=False)
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    def test_storage_layout_creates_and_protects_posix_folders(self, _is_mounted, _mount_permissions):
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
    @mock.patch.object(nasberrypi, "is_mounted", return_value=True)
    def test_storage_layout_rejects_symlink_without_touching_target(self, _is_mounted, _mount_permissions):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as target:
            (Path(directory) / "Private").symlink_to(target, target_is_directory=True)
            owner = mock.Mock(pw_uid=Path(directory).stat().st_uid, pw_gid=Path(directory).stat().st_gid)
            with mock.patch.object(nasberrypi, "MOUNT_POINT", directory), mock.patch.object(nasberrypi, "SHARE_USER", "kali"), mock.patch.object(nasberrypi.pwd, "getpwnam", return_value=owner):
                self.assertFalse(nasberrypi.ensure_storage_layout())
            self.assertEqual(list(Path(target).iterdir()), [])

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
             mock.patch.object(nasberrypi, "print_connection_info"), \
             mock.patch.object(nasberrypi, "color_enabled", return_value=False), \
             mock.patch("builtins.print") as output:
            self.assertTrue(nasberrypi.doctor())
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        for section in ("SYSTEM", "STORAGE", "SAMBA", "NETWORK", "RESULT"):
            self.assertIn(section, rendered)
        self.assertIn("✔ Result: 22/22 checks passed", rendered)

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
             mock.patch.object(nasberrypi, "print_connection_info"), \
             mock.patch("builtins.print") as output:
            nasberrypi.run_dashboard_action(nasberrypi.doctor)
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertNotIn("DIAGNOSTICS", rendered)
        self.assertIn("Result:", rendered)

    @mock.patch.object(nasberrypi, "clear")
    def test_action_feedback_does_not_add_extra_blank_line(self, _clear):
        with mock.patch("builtins.print") as output:
            nasberrypi.show_action_feedback("Setup / change drive", "prompt")
        self.assertEqual(output.call_count, 1)

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
    @mock.patch.object(nasberrypi, "service_active", return_value=True)
    @mock.patch.object(nasberrypi, "menu_mount_status", return_value=("● mounted in NAS mode", "/mnt/nasberry"))
    @mock.patch.object(nasberrypi, "device_exists", return_value=True)
    def test_menu_status_shows_multiple_shared_folders(self, _device, _mount, _sharing, _usage):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            rendered = "\n".join(nasberrypi.menu_status_lines())
        self.assertIn("1 enabled share(s)", rendered)
        self.assertIn("Mount point  /mnt/nasberry", rendered)
        self.assertIn("Share user   kali", rendered)
        self.assertIn("Multiple shared folders", rendered)

    @mock.patch("builtins.print")
    @mock.patch.object(nasberrypi, "show_menu_exit")
    @mock.patch.object(nasberrypi, "render_menu", return_value="dashboard")
    @mock.patch.object(nasberrypi, "read_menu_key", return_value="q")
    @mock.patch.object(nasberrypi, "clear")
    def test_menu_q_exits_cleanly(self, _clear, _read_key, _render, show_exit, _print):
        with mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
            self.assertFalse(nasberrypi.state["running"])
        show_exit.assert_called_once_with()

    @mock.patch("builtins.print")
    @mock.patch.object(nasberrypi, "show_menu_exit")
    @mock.patch.object(nasberrypi, "render_menu", return_value="dashboard")
    @mock.patch.object(nasberrypi, "read_menu_key", return_value="\x03")
    @mock.patch.object(nasberrypi, "clear")
    def test_menu_ctrl_c_key_exits_cleanly(self, _clear, _read_key, _render, show_exit, _print):
        with mock.patch.dict(nasberrypi.state, {"running": True}):
            nasberrypi.menu()
            self.assertFalse(nasberrypi.state["running"])
        show_exit.assert_called_once_with()

    @mock.patch("builtins.print")
    @mock.patch.object(nasberrypi, "show_menu_exit")
    @mock.patch.object(nasberrypi, "render_menu", return_value="dashboard")
    @mock.patch.object(nasberrypi, "read_menu_key", side_effect=KeyboardInterrupt)
    @mock.patch.object(nasberrypi, "clear")
    def test_menu_keyboard_interrupt_exits_cleanly(self, _clear, _read_key, _render, show_exit, _print):
        with mock.patch.dict(nasberrypi.state, {"running": True}):
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

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    def test_lsblk_devices_excludes_swap_and_zram(self, _command_exists, run):
        run.return_value.returncode = 0
        run.return_value.stdout = """{"blockdevices": [
            {"path": "/dev/sda1", "type": "part", "fstype": "exfat", "mountpoints": [null], "rm": true},
            {"path": "/dev/zram0", "type": "disk", "fstype": "swap", "mountpoints": ["[SWAP]"], "rm": false}
        ]}"""
        self.assertEqual([item["path"] for item in nasberrypi.lsblk_devices()], ["/dev/sda1"])

    @mock.patch.object(nasberrypi, "restart_samba_service", return_value=True)
    @mock.patch.object(nasberrypi, "configure_samba_share", return_value=True)
    @mock.patch.object(nasberrypi, "ensure_share_folders", return_value=True)
    @mock.patch.object(nasberrypi, "samba_config_preflight", return_value=True)
    @mock.patch.object(nasberrypi, "ensure_storage_layout", return_value=True)
    @mock.patch.object(nasberrypi, "mount_storage", return_value=True)
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_mounts_repairs_and_restarts(
        self, _geteuid, mount_storage, ensure_layout, _preflight, ensure_shares, configure, restart
    ):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            self.assertTrue(nasberrypi.repair_samba_share())
        mount_storage.assert_called_once_with(repair_permissions=True)
        ensure_layout.assert_called_once_with()
        ensure_shares.assert_called_once_with()
        configure.assert_called_once_with()
        restart.assert_called_once_with()

    @mock.patch.object(nasberrypi, "setup", return_value=True)
    def test_cli_setup_accepts_non_interactive_share_user(self, setup):
        arguments = ["nasberry", "setup", "--non-interactive", "--skip-pin", "--share-user", "kali"]
        with mock.patch.object(sys, "argv", arguments):
            self.assertTrue(nasberrypi.main())
        setup.assert_called_once_with(True, True, "kali")


if __name__ == "__main__":
    unittest.main()
