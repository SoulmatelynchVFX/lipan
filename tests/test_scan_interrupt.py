from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from storage_inspector.__main__ import main
from storage_inspector.domain import InstalledApp
from storage_inspector.scanner import directory_stats
from storage_inspector.windows_inventory import InventoryResult


class InterruptingEntries:
    def __init__(self, entries, after=1):
        self.entries = entries
        self.after = after
        self.visited = 0
        self.closed = False

    def __next__(self):
        if self.visited == self.after:
            raise KeyboardInterrupt
        entry = next(self.entries)
        self.visited += 1
        return entry

    def close(self):
        self.closed = True
        self.entries.close()


class ScanInterruptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("first", "second", "third"):
            (self.root / name).write_bytes(b"abc")

    def test_interruption_retains_observations_and_releases_directory(self):
        entries = InterruptingEntries(os.scandir(self.root))
        with patch("storage_inspector.scanner.os.scandir", return_value=entries):
            result = directory_stats(self.root)
        self.assertEqual((result.file_count, result.logical_bytes), (1, 3))
        self.assertEqual(result.stop_reason, "interrupted")
        self.assertTrue(result.partial)
        self.assertEqual(result.error_count, 0)
        self.assertTrue(entries.closed)
        self.assertTrue(all(path.read_bytes() == b"abc" for path in self.root.iterdir()))

    def test_interrupt_before_first_stat_is_not_empty_success(self):
        with patch.object(Path, "lstat", side_effect=KeyboardInterrupt):
            result = directory_stats(self.root)
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.stop_reason, "interrupted")
        self.assertTrue(result.partial)

    def test_nested_iterators_are_all_closed(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "deeper").mkdir()
        (nested / "deeper/file").write_bytes(b"abc")
        original_scandir = os.scandir
        opened = []

        def open_directory(path):
            entries = InterruptingEntries(original_scandir(path), after=1 if path.name == "deeper" else None)
            opened.append(entries)
            return entries

        with patch("storage_inspector.scanner.os.scandir", side_effect=open_directory):
            result = directory_stats(nested)
        self.assertEqual(result.stop_reason, "interrupted")
        self.assertEqual(result.file_count, 1)
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(entries.closed for entries in opened))

    def test_path_cli_emits_json_and_interrupt_exit_code(self):
        entries = InterruptingEntries(os.scandir(self.root))
        output, errors = io.StringIO(), io.StringIO()
        with patch("storage_inspector.scanner.os.scandir", return_value=entries), \
                redirect_stdout(output), redirect_stderr(errors):
            status = main(["--path", str(self.root)])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 130)
        self.assertEqual(report["stop_reason"], "interrupted")
        self.assertEqual(report["logical_bytes"], 3)
        self.assertTrue(report["partial"])
        self.assertEqual(errors.getvalue(), "")

    @unittest.skipUnless(os.name == "nt", "Windows application selection")
    def test_app_cli_propagates_interrupted_scan(self):
        inventory = InventoryResult(applications=[InstalledApp("sample", "Sample", install_location=str(self.root))])
        inventory.record_error("HKCU", "open_root", PermissionError("denied"))
        entries = InterruptingEntries(os.scandir(self.root))
        output = io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), \
                patch("storage_inspector.scanner.os.scandir", return_value=entries), redirect_stdout(output):
            status = main(["--app", "sample"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 130)
        self.assertEqual(report["scan"]["stop_reason"], "interrupted")
        self.assertEqual(report["scan"]["logical_bytes"], 3)
        self.assertTrue(report["partial"])
        self.assertEqual(report["inventory"]["error_count"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows inventory")
    def test_interrupt_during_discovery_does_not_fabricate_scan(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", side_effect=KeyboardInterrupt), \
                redirect_stdout(output), redirect_stderr(errors):
            status = main(["--list-apps"])
        self.assertEqual(status, 130)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("中断", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
