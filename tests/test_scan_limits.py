from contextlib import redirect_stdout
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


class TrackedEntries:
    def __init__(self, entries, fail_after=None):
        self.entries = entries
        self.fail_after = fail_after
        self.visited = 0
        self.closed = False

    def __next__(self):
        if self.visited == self.fail_after:
            raise OSError("enumeration failed")
        entry = next(self.entries)
        self.visited += 1
        return entry

    def close(self):
        self.closed = True
        self.entries.close()


class ScanLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for index in range(12):
            (self.root / f"{index}.bin").write_bytes(b"abc")

    def test_file_limit_stops_early_without_enumerating_whole_directory(self):
        entries = TrackedEntries(os.scandir(self.root))
        with patch("storage_inspector.scanner.os.scandir", return_value=entries):
            result = directory_stats(self.root, max_files=2)
        self.assertEqual((result.file_count, result.logical_bytes), (2, 6))
        self.assertEqual(result.stop_reason, "max_files")
        self.assertTrue(result.partial)
        self.assertEqual(entries.visited, 2)
        self.assertTrue(entries.closed)

    def test_exact_limit_is_conservatively_partial(self):
        result = directory_stats(self.root / "0.bin", max_files=1)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.stop_reason, "max_files")
        self.assertTrue(result.partial)

    def test_finished_before_limits_is_complete(self):
        result = directory_stats(self.root, max_files=20, max_seconds=60)
        self.assertEqual(result.file_count, 12)
        self.assertIsNone(result.stop_reason)
        self.assertFalse(result.partial)

    def test_time_limit_returns_partial_and_closes_iterator(self):
        entries = TrackedEntries(os.scandir(self.root))
        with patch("storage_inspector.scanner.os.scandir", return_value=entries), \
                patch("storage_inspector.scanner.time.monotonic", side_effect=[0, 0, 0, 2]):
            result = directory_stats(self.root, max_seconds=1)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.stop_reason, "max_seconds")
        self.assertTrue(result.partial)
        self.assertTrue(entries.closed)

    def test_nested_active_directories_close_on_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested/file").write_bytes(b"abc")
            real_scandir = os.scandir
            opened = []

            def track(path):
                entries = TrackedEntries(real_scandir(path))
                opened.append(entries)
                return entries

            with patch("storage_inspector.scanner.os.scandir", side_effect=track):
                result = directory_stats(root, max_files=1)
            self.assertEqual(result.file_count, 1)
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(entries.closed for entries in opened))

    def test_enumeration_failure_retains_counts_and_closes_iterator(self):
        entries = TrackedEntries(os.scandir(self.root), fail_after=1)
        with patch("storage_inspector.scanner.os.scandir", return_value=entries):
            result = directory_stats(self.root)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.error_count, 1)
        self.assertTrue(result.partial)
        self.assertIsNone(result.stop_reason)
        self.assertTrue(entries.closed)

    def test_invalid_limits_fail_before_filesystem_access(self):
        for limits in [{"max_files": value} for value in (0, -1, 1.5, True)] + [
            {"max_seconds": value} for value in (0, -1, float("nan"), float("inf"), True)
        ]:
            with self.subTest(limits=limits), patch.object(Path, "lstat") as probe:
                with self.assertRaises(ValueError):
                    directory_stats(self.root, **limits)
                probe.assert_not_called()

    def test_cli_limits_produce_json_partial_result(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--path", str(self.root), "--max-files", "2", "--max-seconds", "60"])
        self.assertEqual(status, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["stop_reason"], "max_files")
        self.assertTrue(result["partial"])

    def test_cli_rejects_bad_limits_and_list_only_mode(self):
        for args in [["--path", str(self.root), "--max-files", "0"],
                     ["--path", str(self.root), "--max-seconds", "nan"],
                     ["--path", str(self.root), "--max-seconds", "inf"],
                     ["--list-apps", "--max-files", "1"]]:
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO), \
                    patch("storage_inspector.__main__.discover_inventory") as discover:
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
                discover.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows application path")
    def test_app_mode_propagates_limit_and_partial_status(self):
        inventory = InventoryResult(applications=[InstalledApp("sample", "Sample", install_location=str(self.root))])
        output = io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), redirect_stdout(output):
            status = main(["--app", "sample", "--max-files", "2"])
        result = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue(result["partial"])
        self.assertEqual(result["scan"]["stop_reason"], "max_files")
        self.assertEqual(result["scan"]["file_count"], 2)


if __name__ == "__main__":
    unittest.main()
