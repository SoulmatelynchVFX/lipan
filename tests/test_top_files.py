from contextlib import redirect_stderr, redirect_stdout
import heapq
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from storage_inspector.__main__ import main
from storage_inspector.app_scan import inspect_app
from storage_inspector.domain import InstalledApp
from storage_inspector.scanner import directory_stats
from storage_inspector.windows_inventory import InventoryResult


class TopFilesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def file(self, name, size):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_nested_ranking_does_not_limit_totals_or_read_contents(self):
        self.file("small", 1)
        large = self.file("nested/large", 9)
        medium = self.file("medium", 4)
        with patch.object(Path, "open", side_effect=AssertionError("must not read contents")):
            result = directory_stats(self.root, top_files=2)
        self.assertEqual(result.largest_files, [
            {"path": str(large), "logical_bytes": 9},
            {"path": str(medium), "logical_bytes": 4},
        ])
        self.assertEqual((result.file_count, result.logical_bytes), (3, 14))
        self.assertFalse(result.partial)

    def test_ties_and_zero_size_are_sorted_by_descending_path(self):
        paths = [self.file(name, 0) for name in ["a", "c", "b"]]
        result = directory_stats(self.root, top_files=2)
        self.assertEqual([item["path"] for item in result.largest_files],
                         sorted(map(str, paths), reverse=True)[:2])

    def test_disabled_collection_and_empty_directory(self):
        self.assertEqual(directory_stats(self.root, top_files=5).largest_files, [])
        self.file("sample", 3)
        with patch("storage_inspector.scanner.heapq.heappush") as push:
            self.assertEqual(directory_stats(self.root).largest_files, [])
        push.assert_not_called()

    def test_single_file_and_missing_path(self):
        path = self.file("sample", 3)
        self.assertEqual(directory_stats(path, top_files=5).largest_files,
                         [{"path": str(path), "logical_bytes": 3}])
        missing = directory_stats(self.root / "missing", top_files=5)
        self.assertTrue(missing.partial)
        self.assertEqual(missing.largest_files, [])

    def test_hardlinks_are_ranked_once(self):
        path = self.file("original", 7)
        os.link(path, self.root / "alias")
        result = directory_stats(self.root, top_files=5)
        self.assertEqual((result.file_count, result.duplicates), (1, 1))
        self.assertEqual(len(result.largest_files), 1)
        self.assertEqual(result.largest_files[0]["logical_bytes"], 7)

    def test_skipped_files_are_not_ranked(self):
        self.file("sample", 3)
        with patch("storage_inspector.scanner.skip_reason", side_effect=[None, "offline"]):
            result = directory_stats(self.root, top_files=5)
        self.assertEqual(result.largest_files, [])
        self.assertEqual(result.skipped, 1)
        self.assertTrue(result.partial)

    def test_candidates_remain_bounded(self):
        for number in range(10):
            self.file(str(number), number)
        original_push, original_pushpop = heapq.heappush, heapq.heappushpop

        def push(heap, item):
            self.assertLess(len(heap), 3)
            return original_push(heap, item)

        def pushpop(heap, item):
            self.assertEqual(len(heap), 3)
            return original_pushpop(heap, item)

        with patch("storage_inspector.scanner.heapq.heappush", side_effect=push), \
                patch("storage_inspector.scanner.heapq.heappushpop", side_effect=pushpop):
            result = directory_stats(self.root, top_files=3)
        self.assertEqual([item["logical_bytes"] for item in result.largest_files], [9, 8, 7])

    def test_limits_return_only_observed_ranking(self):
        self.file("first", 3)
        self.file("second", 9)
        result = directory_stats(self.root, top_files=5, max_files=1)
        self.assertTrue(result.partial)
        self.assertEqual(result.stop_reason, "max_files")
        self.assertEqual(len(result.largest_files), 1)
        self.assertEqual(result.largest_files[0]["logical_bytes"], result.logical_bytes)
        with patch("storage_inspector.scanner.time.monotonic", side_effect=[0, 0, 0, 2]):
            result = directory_stats(self.root, top_files=5, max_seconds=1)
        self.assertEqual(result.stop_reason, "max_seconds")
        self.assertEqual(len(result.largest_files), 1)

    def test_interruption_preserves_ranking_and_closes_iterator(self):
        path = self.file("sample", 3)

        class InterruptedEntries:
            closed = False

            def __init__(self):
                self.entries = iter([path])

            def __next__(self):
                try:
                    return next(self.entries)
                except StopIteration:
                    raise KeyboardInterrupt

            def close(self):
                self.closed = True

        entries = InterruptedEntries()
        output = io.StringIO()
        with patch("storage_inspector.scanner.os.scandir", return_value=entries), redirect_stdout(output):
            self.assertEqual(main(["--path", str(self.root), "--top-files", "2"]), 130)
        report = json.loads(output.getvalue())
        self.assertEqual(report["largest_files"], [{"path": str(path), "logical_bytes": 3}])
        self.assertEqual(report["stop_reason"], "interrupted")
        self.assertTrue(report["partial"])
        self.assertTrue(entries.closed)

    def test_invalid_top_files_rejected_before_io(self):
        for value in [0, -1, True, 1.5, "2"]:
            with self.subTest(value=value), patch.object(Path, "lstat") as lstat:
                with self.assertRaises(ValueError):
                    directory_stats(self.root, top_files=value)
                with self.assertRaises(ValueError):
                    inspect_app(InventoryResult(), "unknown", top_files=value)
                lstat.assert_not_called()

    def test_cli_invalid_or_inventory_options_do_not_discover_or_scan(self):
        for arguments in [["--path", str(self.root), "--top-files", "0"],
                          ["--app", "selected", "--top-files", "-1"],
                          ["--path", str(self.root), "--top-files", "1.5"],
                          ["--list-apps", "--top-files", "3"]]:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), \
                    patch("storage_inspector.__main__.discover_inventory") as discover, \
                    patch("storage_inspector.__main__.directory_stats") as scan:
                with self.assertRaises(SystemExit) as raised:
                    main(arguments)
                self.assertEqual(raised.exception.code, 2)
                discover.assert_not_called()
                scan.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows installation paths")
    def test_cli_app_propagates_ranking(self):
        path = self.file("sample", 3)
        inventory = InventoryResult(applications=[
            InstalledApp("selected", "Example", install_location=str(self.root)),
        ])
        output = io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), redirect_stdout(output):
            self.assertEqual(main(["--app", "selected", "--top-files", "1"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["scan"]["largest_files"], [{"path": str(path), "logical_bytes": 3}])
        self.assertFalse(report["partial"])


if __name__ == "__main__":
    unittest.main()
