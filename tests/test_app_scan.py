from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from storage_inspector.app_scan import inspect_app, valid_install_location
from storage_inspector.domain import InstalledApp
from storage_inspector.scanner import DirectoryStats
from storage_inspector.windows_inventory import InventoryResult
from storage_inspector.__main__ import main


class AppScanTests(unittest.TestCase):
    def inventory(self, path="C:\\Example", app_id="selected"):
        return InventoryResult(applications=[InstalledApp(app_id, "Example", install_location=path)])

    def test_local_absolute_paths_only(self):
        for path in ["C:\\Example", "D:/Tools/Editor", "C:\\应用目录"]:
            self.assertTrue(valid_install_location(path), path)
        for path in ["", ".", "Tools", "C:Tools", "\\Tools", "C:\\", "C:/",
                     "C:\\Tools\\..", "%ProgramFiles%\\Editor", "C:\\%VAR%\\Editor",
                     "\\\\server\\share\\Editor", "\\\\?\\C:\\Editor", "\\\\.\\C:",
                     "C:\\a:stream", "C:\\a*", "C:\\a\x00b", '"C:\\Editor"',
                     "C:\\ ", "C:\\...", "C:\\a\\.. ", "C:\\a\nb"]:
            self.assertFalse(valid_install_location(path), path)

    def test_invalid_selection_never_scans(self):
        inventories = [self.inventory(""), self.inventory("C:\\"),
                       self.inventory(app_id="other"), self.inventory("relative"),
                       InventoryResult(applications=self.inventory().applications * 2)]
        for inventory in inventories:
            with self.subTest(inventory=inventory), patch("storage_inspector.app_scan.directory_stats") as scan:
                result = inspect_app(inventory, "selected")
                self.assertTrue(result["partial"])
                self.assertIsNone(result["scan"])
                self.assertIn("error", result)
                scan.assert_not_called()

    def test_non_directory_and_reparse_root_never_scans(self):
        for mode, attributes in [(stat.S_IFREG, 0), (stat.S_IFDIR, 0x400), (stat.S_IFDIR, 0x1000)]:
            with self.subTest(mode=mode, attributes=attributes), \
                    patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode, st_file_attributes=attributes)), \
                    patch("storage_inspector.app_scan.directory_stats") as scan:
                self.assertTrue(inspect_app(self.inventory(), "selected")["partial"])
                scan.assert_not_called()

    def test_missing_or_inaccessible_root_does_not_look_empty(self):
        for error in [FileNotFoundError("missing"), PermissionError("denied")]:
            with patch.object(Path, "lstat", side_effect=error):
                result = inspect_app(self.inventory(), "selected")
            self.assertTrue(result["partial"])
            self.assertIsNone(result["scan"])

    def test_inventory_and_scan_errors_propagate(self):
        for inventory_error, scan_error in [(True, False), (False, True), (False, False)]:
            inventory = self.inventory()
            if inventory_error:
                inventory.record_error("HKCU", "open_root", PermissionError("denied"))
            stats = DirectoryStats("C:\\Example", logical_bytes=10, error_count=int(scan_error))
            with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR)), \
                    patch("storage_inspector.app_scan.directory_stats", return_value=stats) as scan:
                result = inspect_app(inventory, "selected")
            self.assertEqual(result["partial"], inventory_error or scan_error)
            self.assertEqual(result["scan"]["logical_bytes"], 10)
            self.assertEqual(result["scope"], "install_location_only")
            scan.assert_called_once_with(Path("C:\\Example"), max_files=None, max_seconds=None,
                                         top_files=None, exclude_dirs=[], by_extension=False)

    @unittest.skipUnless(os.name == "nt", "Windows installation paths")
    def test_cli_scans_only_selected_synthetic_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chosen").mkdir()
            (root / "other").mkdir()
            fixture = root / "chosen/file"
            fixture.write_bytes(b"hello")
            (root / "other/file").write_bytes(b"not selected")
            inventory = self.inventory(str(root / "chosen"))
            inventory.applications.extend(self.inventory(str(root / "other"), "other").applications)
            output = io.StringIO()
            with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), redirect_stdout(output):
                self.assertEqual(main(["--app", "selected"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["scan"]["logical_bytes"], 5)
            self.assertEqual(report["scan"]["file_count"], 1)
            self.assertEqual(fixture.read_bytes(), b"hello")

    @unittest.skipUnless(os.name == "nt", "Windows CLI")
    def test_cli_unknown_and_empty_id_return_json_error(self):
        for app_id in ["unknown", ""]:
            output = io.StringIO()
            with patch("storage_inspector.__main__.discover_inventory", return_value=self.inventory()), redirect_stdout(output):
                self.assertEqual(main(["--app", app_id]), 2)
            self.assertTrue(json.loads(output.getvalue())["partial"])


if __name__ == "__main__":
    unittest.main()
