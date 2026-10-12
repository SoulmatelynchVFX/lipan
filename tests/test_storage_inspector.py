from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from storage_inspector.__main__ import main
from storage_inspector.scanner import DirectoryStats, directory_stats, should_skip
from storage_inspector.windows_inventory import discover_installed_apps, _read_values, _stable_id


class StorageInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_nested_files_and_empty_directory(self):
        self.assertEqual(directory_stats(self.root).file_count, 0)
        (self.root / "child").mkdir()
        (self.root / "first.bin").write_bytes(b"abc")
        (self.root / "child/second.bin").write_bytes(b"12345")
        result = directory_stats(self.root)
        self.assertEqual((result.logical_bytes, result.file_count), (8, 2))
        self.assertFalse(result.partial)

    def test_single_file_and_missing_path(self):
        path = self.root / "file"
        path.write_bytes(b"abc")
        self.assertEqual(directory_stats(path).logical_bytes, 3)
        missing = directory_stats(self.root / "missing")
        self.assertTrue(missing.partial)
        self.assertEqual(missing.error_count, 1)

    def test_hardlinks_count_once(self):
        path = self.root / "original"
        path.write_bytes(b"abc")
        os.link(path, self.root / "alias")
        result = directory_stats(self.root)
        self.assertEqual((result.logical_bytes, result.file_count, result.duplicates), (3, 1, 1))

    def test_reparse_and_offline_attributes_are_skipped(self):
        for flag in (0x400, 0x1000, 0x40000, 0x400000):
            with self.subTest(flag=flag):
                info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=flag)
                with patch.object(Path, "lstat", return_value=info), patch("os.scandir") as walk:
                    result = directory_stats(self.root)
                    self.assertEqual(result.skipped, 1)
                    self.assertTrue(result.partial)
                    walk.assert_not_called()
        self.assertTrue(should_skip(SimpleNamespace(st_mode=stat.S_IFLNK)))

    def test_permission_failure_is_not_empty_success(self):
        with patch("os.scandir", side_effect=PermissionError("denied")):
            result = directory_stats(self.root)
        self.assertEqual(result.error_count, 1)
        self.assertTrue(result.partial)
        self.assertIn("denied", result.errors[0]["message"])

    def test_error_details_are_bounded_but_count_is_not(self):
        result = DirectoryStats("test")
        for _ in range(105):
            result.record_error("test", OSError("denied"))
        self.assertEqual((result.error_count, len(result.errors)), (105, 100))

    def test_cli_is_explicit(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                main([])
        self.assertEqual(error.exception.code, 2)

    def test_cli_scan_emits_json_and_does_not_modify_fixture(self):
        path = self.root / "file"
        path.write_bytes(b"hello")
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--path", str(self.root)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["logical_bytes"], 5)
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertEqual(path.read_bytes(), b"hello")

    def test_cli_partial_exit_code(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--path", str(self.root / "missing")]), 2)

    def test_inventory_on_non_windows(self):
        with patch("storage_inspector.windows_inventory.os.name", "posix"):
            self.assertEqual(discover_installed_apps(), [])
        self.assertEqual(_stable_id("App", "Maker"), _stable_id("App", "Maker"))

    def test_inventory_deduplicates_and_prefers_install_location(self):
        records = [
            {"DisplayName": "Editor", "Publisher": "Maker"},
            {"DisplayName": "Editor", "Publisher": "Maker", "InstallLocation": '"D:\\Editor"',
             "DisplayVersion": "1.0", "UninstallString": "never-execute.exe"},
            {"DisplayName": "Hidden", "SystemComponent": 1},
            {"Publisher": "No display name"},
        ]

        root_handle = nullcontext("root")

        def open_key(hive, key):
            if hive is root_handle:
                return nullcontext(int(key))
            if hive == 1 and "WOW6432Node" not in key:
                return root_handle
            raise PermissionError("unavailable registry view")

        registry = SimpleNamespace(
            HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2, OpenKey=open_key,
            QueryInfoKey=lambda key: (len(records), 0, 0),
            EnumKey=lambda key, index: str(index),
        )
        with patch.dict("sys.modules", {"winreg": registry}), \
                patch("storage_inspector.windows_inventory.os.name", "nt"), \
                patch("storage_inspector.windows_inventory._read_values", side_effect=lambda key: records[key]):
            apps = discover_installed_apps()
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].install_location, "D:\\Editor")
        self.assertEqual(apps[0].version, "1.0")
        self.assertEqual(apps[0].uninstall_command, "never-execute.exe")

    def test_registry_value_errors_are_skipped(self):
        def enum_value(key, index):
            if index == 0:
                raise OSError("unreadable value")
            return "DisplayName", "Editor", 1

        registry = SimpleNamespace(QueryInfoKey=lambda key: (0, 2, 0), EnumValue=enum_value)
        with patch.dict("sys.modules", {"winreg": registry}):
            self.assertEqual(_read_values("key"), {"DisplayName": "Editor"})


if __name__ == "__main__":
    unittest.main()
