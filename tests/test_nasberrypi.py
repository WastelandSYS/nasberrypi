import importlib.util
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("nasberrypi", Path(__file__).parents[1] / "nasberrypi.py")
nasberrypi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nasberrypi)


class NasberryTests(unittest.TestCase):
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

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    def test_samba_config_rejects_wrong_path(self, _command_exists, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "/somewhere/else\n"
        valid, reason = nasberrypi.samba_config_valid()
        self.assertFalse(valid)
        self.assertIn("not", reason)
        run.assert_called_once_with([
            "testparm",
            "-s",
            "--section-name=Nasberry",
            "--parameter-name=path",
        ])

    @mock.patch.object(nasberrypi, "run")
    @mock.patch.object(nasberrypi, "command_exists", return_value=True)
    def test_samba_config_uses_section_name_instead_of_config_filename(self, _command_exists, run):
        run.return_value.returncode = 0
        run.return_value.stdout = f"{nasberrypi.MOUNT_POINT}\n"
        valid, _reason = nasberrypi.samba_config_valid()
        self.assertTrue(valid)
        self.assertNotIn(nasberrypi.SHARE_NAME, run.call_args.args[0][1:])
        self.assertIn(f"--section-name={nasberrypi.SHARE_NAME}", run.call_args.args[0])

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

    @mock.patch.object(nasberrypi, "service_exists", return_value=False)
    @mock.patch.object(nasberrypi, "configure_samba_share", return_value=True)
    @mock.patch.object(nasberrypi.os, "geteuid", return_value=0)
    def test_repair_samba_share_recreates_and_validates_share(self, _geteuid, configure, _service_exists):
        with mock.patch.object(nasberrypi, "SHARE_USER", "kali"):
            self.assertTrue(nasberrypi.repair_samba_share())
        configure.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
