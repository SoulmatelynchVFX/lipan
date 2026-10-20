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


def group(extension, size, count):
    return {"extension": extension, "logical_bytes": size, "file_count": count}


class ExtensionSummaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def file(self, name, size=3):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def assert_totals(self, result):
        self.assertEqual(sum(item["logical_bytes"] for item in result.extension_summary), result.logical_bytes)
        self.assertEqual(sum(item["file_count"] for item in result.extension_summary), result.file_count)

    def test_groups_nested_files_case_insensitively_without_reading_contents(self):
        self.file("first.TXT", 3)
        self.file("nested/second.txt", 4)
        self.file("video.mp4", 10)
        self.file("notes.资料", 2)
        with patch.object(Path, "open", side_effect=AssertionError("must not read contents")):
            result = directory_stats(self.root, by_extension=True)
        self.assertEqual(result.extension_summary, [group(".mp4", 10, 1), group(".txt", 7, 2), group(".资料", 2, 1)])
        self.assertFalse(result.partial)
        self.assert_totals(result)

    def test_last_suffix_and_extensionless_files(self):
        for name in ("README", ".env", "folder.with.dot/plain"):
            self.file(name)
        self.file("archive.tar.gz", 4)
        self.file(".config.json", 2)
        result = directory_stats(self.root, by_extension=True)
        self.assertEqual(result.extension_summary, [group("", 9, 3), group(".gz", 4, 1), group(".json", 2, 1)])
        self.assert_totals(result)

    def test_ties_sorted_by_extension_and_zero_length_retained(self):
        for name in ("file.z", "file.a", "no_suffix"):
            self.file(name, 0)
        result = directory_stats(self.root, by_extension=True)
        self.assertEqual(result.extension_summary, [group("", 0, 1), group(".a", 0, 1), group(".z", 0, 1)])
        self.assert_totals(result)

    def test_disabled_and_empty_results(self):
        self.assertEqual(directory_stats(self.root, by_extension=True).extension_summary, [])
        self.file("sample.txt")
        result = directory_stats(self.root)
        self.assertEqual(result.extension_summary, [])
        self.assertEqual(result.file_count, 1)

    def test_single_file_and_missing_root(self):
        path = self.file("sample.TXT")
        self.assertEqual(directory_stats(path, by_extension=True).extension_summary, [group(".txt", 3, 1)])
        missing = directory_stats(self.root / "missing", by_extension=True)
        self.assertTrue(missing.partial)
        self.assertEqual(missing.extension_summary, [])

    def test_hardlink_uses_first_counted_name_only(self):
        original = self.file("original.txt")
        alias = self.root / "alias.bin"
        os.link(original, alias)
        with patch("storage_inspector.scanner.os.scandir", return_value=iter([alias, original])):
            result = directory_stats(self.root, by_extension=True)
        self.assertEqual(result.extension_summary, [group(".bin", 3, 1)])
        self.assertEqual(result.duplicates, 1)
        self.assert_totals(result)

    def test_excluded_and_skipped_items_do_not_contribute(self):
        kept = self.file("kept.txt")
        skipped = self.file("offline.bin", 20)
        self.file("cache/big.dat", 50)
        with patch("storage_inspector.scanner.os.scandir",
                   return_value=iter([self.root / "cache", skipped, kept])), \
                patch("storage_inspector.scanner.skip_reason", side_effect=[None, None, "offline", None]):
            result = directory_stats(self.root, exclude_dirs=["cache"], by_extension=True, top_files=1)
        self.assertEqual(result.extension_summary, [group(".txt", 3, 1)])
        self.assertEqual((result.excluded_directories, result.skipped), (1, 1))
        self.assertEqual(result.largest_files, [{"path": str(kept), "logical_bytes": 3}])
        self.assertTrue(result.partial)
        self.assert_totals(result)

    def test_metadata_error_keeps_other_groups(self):
        good = self.file("good.txt")
        missing = self.root / "vanished.bin"
        with patch("storage_inspector.scanner.os.scandir", return_value=iter([missing, good])):
            result = directory_stats(self.root, by_extension=True)
        self.assertEqual(result.extension_summary, [group(".txt", 3, 1)])
        self.assertEqual(result.error_count, 1)
        self.assertTrue(result.partial)
        self.assert_totals(result)

    def test_file_and_time_limits_keep_only_observed_groups(self):
        first = self.file("first.txt")
        second = self.file("second.bin")
        with patch("storage_inspector.scanner.os.scandir", return_value=iter([first, second])):
            result = directory_stats(self.root, by_extension=True, max_files=1)
        self.assertEqual(result.extension_summary, [group(".txt", 3, 1)])
        self.assertEqual(result.stop_reason, "max_files")
        self.assert_totals(result)
        with patch("storage_inspector.scanner.os.scandir", return_value=iter([first, second])), \
                patch("storage_inspector.scanner.time.monotonic", side_effect=[0, 0, 0, 2]):
            result = directory_stats(self.root, by_extension=True, max_seconds=1)
        self.assertEqual(result.extension_summary, [group(".txt", 3, 1)])
        self.assertEqual(result.stop_reason, "max_seconds")
        self.assert_totals(result)

    def test_cli_interrupt_keeps_summary_and_closes_iterator(self):
        first = self.file("first.txt")

        class Entries:
            closed = False

            def __init__(self):
                self.entries = iter([first])

            def __next__(self):
                try:
                    return next(self.entries)
                except StopIteration:
                    raise KeyboardInterrupt

            def close(self):
                self.closed = True

        entries = Entries()
        output = io.StringIO()
        with patch("storage_inspector.scanner.os.scandir", return_value=entries), redirect_stdout(output):
            status = main(["--path", str(self.root), "--by-extension"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 130)
        self.assertEqual(report["extension_summary"], [group(".txt", 3, 1)])
        self.assertEqual(report["stop_reason"], "interrupted")
        self.assertTrue(report["partial"])
        self.assertTrue(entries.closed)

    def test_cli_list_apps_rejects_flag_before_discovery(self):
        with redirect_stderr(io.StringIO()), patch("storage_inspector.__main__.discover_inventory") as discover:
            with self.assertRaises(SystemExit) as raised:
                main(["--list-apps", "--by-extension"])
        self.assertEqual(raised.exception.code, 2)
        discover.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows installation paths")
    def test_cli_app_propagates_summary_and_exclusions(self):
        self.file("kept.txt")
        self.file("cache/excluded.bin", 50)
        inventory = InventoryResult(applications=[
            InstalledApp("sample", "Sample", install_location=str(self.root)),
        ])
        output = io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), redirect_stdout(output):
            status = main(["--app", "sample", "--by-extension", "--exclude-dir", "cache"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue(report["partial"])
        self.assertEqual(report["scan"]["extension_summary"], [group(".txt", 3, 1)])


if __name__ == "__main__":
    unittest.main()
