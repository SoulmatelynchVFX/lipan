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


def group(path, size, count, kind="directory"):
    return {"path": str(path), "kind": kind, "logical_bytes": size, "file_count": count}


class DirectorySummaryTests(unittest.TestCase):
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
        self.assertEqual(sum(item["logical_bytes"] for item in result.directory_summary), result.logical_bytes)
        self.assertEqual(sum(item["file_count"] for item in result.directory_summary), result.file_count)

    def test_nested_groups_and_direct_files_use_single_metadata_walk(self):
        self.file("direct.txt", 2)
        self.file("other.txt", 3)
        self.file("alpha/one.bin", 7)
        self.file("alpha/nested/two.bin", 8)
        self.file("beta/file", 4)
        opened = []
        original_scandir = os.scandir

        def track(path):
            opened.append(path)
            return original_scandir(path)

        with patch("storage_inspector.scanner.os.scandir", side_effect=track), \
                patch.object(Path, "open", side_effect=AssertionError("must not read contents")):
            result = directory_stats(self.root, by_directory=True, by_extension=True, top_files=2)
        self.assertEqual(result.directory_summary, [
            group(self.root / "alpha", 15, 2), group(self.root, 5, 2, "direct_files"),
            group(self.root / "beta", 4, 1),
        ])
        self.assertEqual(len(opened), 4)
        self.assertEqual(len(set(opened)), 4)
        self.assertFalse(result.partial)
        self.assertEqual(sum(item["logical_bytes"] for item in result.extension_summary), result.logical_bytes)
        self.assertEqual([item["logical_bytes"] for item in result.largest_files], [8, 7])
        self.assert_totals(result)

    def test_empty_and_disabled_results(self):
        (self.root / "empty").mkdir()
        self.assertEqual(directory_stats(self.root, by_directory=True).directory_summary, [])
        self.file("child/file")
        result = directory_stats(self.root)
        self.assertEqual(result.directory_summary, [])
        self.assertEqual(result.file_count, 1)

    def test_zero_byte_groups_and_ties_sort_by_path(self):
        self.file("z/file", 0)
        self.file("a/file", 0)
        result = directory_stats(self.root, by_directory=True)
        self.assertEqual(result.directory_summary, [group(self.root / "a", 0, 1), group(self.root / "z", 0, 1)])
        self.assert_totals(result)

    def test_single_file_and_missing_root(self):
        path = self.file("single")
        self.assertEqual(directory_stats(path, by_directory=True).directory_summary, [group(path, 3, 1, "file")])
        missing = directory_stats(self.root / "missing", by_directory=True)
        self.assertEqual(missing.directory_summary, [])
        self.assertTrue(missing.partial)

    def test_cross_directory_hardlinks_use_first_counted_path(self):
        original = self.file("a/original")
        alias = self.root / "b/alias"
        alias.parent.mkdir()
        os.link(original, alias)
        entries = {self.root: [alias.parent, original.parent], alias.parent: [alias], original.parent: [original]}
        with patch("storage_inspector.scanner.os.scandir", side_effect=lambda path: iter(entries[path])):
            result = directory_stats(self.root, by_directory=True)
        self.assertEqual(result.directory_summary, [group(alias.parent, 3, 1)])
        self.assertEqual(result.duplicates, 1)
        self.assert_totals(result)

    def test_excluded_subtrees_and_empty_directories_have_no_group(self):
        self.file("cache/large", 100)
        self.file("kept/nested/file")
        (self.root / "empty").mkdir()
        result = directory_stats(self.root, by_directory=True, exclude_dirs=["cache"])
        self.assertEqual(result.directory_summary, [group(self.root / "kept", 3, 1)])
        self.assertEqual(result.excluded_directories, 1)
        self.assertTrue(result.partial)
        self.assert_totals(result)

    def test_skipped_and_unreadable_entries_are_not_zero_groups(self):
        kept = self.file("kept")
        skipped = self.file("offline/file", 30).parent
        missing = self.root / "missing"
        with patch("storage_inspector.scanner.os.scandir", return_value=iter([skipped, missing, kept])), \
                patch("storage_inspector.scanner.skip_reason", side_effect=[None, "offline", None]):
            result = directory_stats(self.root, by_directory=True)
        self.assertEqual(result.directory_summary, [group(self.root, 3, 1, "direct_files")])
        self.assertEqual((result.skipped, result.error_count), (1, 1))
        self.assertTrue(result.partial)
        self.assert_totals(result)

    def test_file_and_time_limits_preserve_partial_groups(self):
        self.file("child/file")
        result = directory_stats(self.root, by_directory=True, max_files=1)
        self.assertEqual(result.directory_summary, [group(self.root / "child", 3, 1)])
        self.assertEqual(result.stop_reason, "max_files")
        self.assert_totals(result)
        with patch("storage_inspector.scanner.time.monotonic", side_effect=[0, 0, 0, 0, 2]):
            result = directory_stats(self.root, by_directory=True, max_seconds=1)
        self.assertEqual(result.directory_summary, [group(self.root / "child", 3, 1)])
        self.assertEqual(result.stop_reason, "max_seconds")
        self.assert_totals(result)

    def test_cli_interruption_retains_groups_and_closes_iterator(self):
        first = self.file("first")

        class Entries:
            closed = False

            def __init__(self):
                self.paths = iter([first])

            def __next__(self):
                try:
                    return next(self.paths)
                except StopIteration:
                    raise KeyboardInterrupt

            def close(self):
                self.closed = True

        entries = Entries()
        output = io.StringIO()
        with patch("storage_inspector.scanner.os.scandir", return_value=entries), redirect_stdout(output):
            status = main(["--path", str(self.root), "--by-directory"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 130)
        self.assertEqual(report["directory_summary"], [group(self.root, 3, 1, "direct_files")])
        self.assertTrue(report["partial"])
        self.assertTrue(entries.closed)

    def test_cli_successful_path_scan(self):
        self.file("资料/file")
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--path", str(self.root), "--by-directory"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertFalse(report["partial"])
        self.assertEqual(report["directory_summary"], [group(self.root / "资料", 3, 1)])

    def test_cli_inventory_rejects_flag_before_discovery(self):
        with redirect_stderr(io.StringIO()), patch("storage_inspector.__main__.discover_inventory") as discover:
            with self.assertRaises(SystemExit) as raised:
                main(["--list-apps", "--by-directory"])
        self.assertEqual(raised.exception.code, 2)
        discover.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows installation paths")
    def test_app_scan_propagates_groups_and_partial_status(self):
        self.file("child/file")
        self.file("cache/file")
        inventory = InventoryResult(applications=[InstalledApp("sample", "Sample", install_location=str(self.root))])
        output = io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), redirect_stdout(output):
            status = main(["--app", "sample", "--by-directory", "--exclude-dir", "cache"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue(report["partial"])
        self.assertEqual(report["scan"]["directory_summary"], [group(self.root / "child", 3, 1)])


if __name__ == "__main__":
    unittest.main()
