from contextlib import redirect_stderr, redirect_stdout
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


class ExcludeDirectoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def file(self, name, content=b"abc"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_prunes_matching_subtrees_at_any_depth(self):
        self.file(".git/objects/data")
        self.file("src/node_modules/package/large", b"x" * 20)
        kept = self.file("src/main.py")
        original_scandir = os.scandir
        opened = []

        def track(path):
            self.assertNotIn(path.name.casefold(), (".git", "node_modules"))
            opened.append(path)
            return original_scandir(path)

        with patch("storage_inspector.scanner.os.scandir", side_effect=track):
            result = directory_stats(self.root, exclude_dirs=[".GIT", "Node_Modules", ".git"], top_files=5)
        self.assertEqual(set(opened), {self.root, self.root / "src"})
        self.assertEqual(result.exclude_dirs, [".git", "node_modules"])
        self.assertEqual(result.excluded_directories, 2)
        self.assertEqual((result.file_count, result.logical_bytes), (1, 3))
        self.assertEqual(result.largest_files, [{"path": str(kept), "logical_bytes": 3}])
        self.assertEqual((result.skipped, result.error_count), (0, 0))
        self.assertIsNone(result.stop_reason)
        self.assertTrue(result.partial)

    def test_selected_root_and_same_named_files_are_not_excluded(self):
        self.file("cache/data")
        result = directory_stats(self.root / "cache", exclude_dirs=["cache"])
        self.assertEqual(result.file_count, 1)
        self.assertFalse(result.partial)
        path = self.file("other/cache")
        result = directory_stats(self.root / "other", exclude_dirs=["cache"])
        self.assertEqual(result.logical_bytes, 3)
        self.assertEqual(result.excluded_directories, 0)
        self.assertFalse(directory_stats(path, exclude_dirs=["cache"]).partial)

    def test_default_and_unmatched_filters_preserve_complete_results(self):
        self.file("cache/data")
        for exclusions in (None, [], ["cache-backup"]):
            with self.subTest(exclusions=exclusions):
                result = directory_stats(self.root, exclude_dirs=exclusions)
                self.assertEqual(result.file_count, 1)
                self.assertEqual(result.excluded_directories, 0)
                self.assertFalse(result.partial)

    def test_empty_excluded_directory_still_marks_partial(self):
        (self.root / "cache").mkdir()
        result = directory_stats(self.root, exclude_dirs=["cache"])
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.excluded_directories, 1)
        self.assertTrue(result.partial)

    def test_nested_matches_count_only_encountered_roots(self):
        self.file("cache/cache/data")
        result = directory_stats(self.root, exclude_dirs=["cache"])
        self.assertEqual(result.excluded_directories, 1)

    def test_links_and_offline_items_remain_skipped_not_excluded(self):
        (self.root / "cache").mkdir()
        with patch("storage_inspector.scanner.skip_reason", side_effect=[None, "offline"]):
            result = directory_stats(self.root, exclude_dirs=["cache"])
        self.assertEqual((result.skipped, result.excluded_directories), (1, 0))

    def test_invalid_names_fail_before_io(self):
        invalid = ["cache", 1, [None], [True], [""], ["."], [".."], ["a/b"],
                   ["a\\b"], ["C:cache"], ["*.tmp"], ["a?"], ["a\n"], ["a\x00"],
                   ["cache."], ["cache "], ["a|b"], ['a"b'], ["a<b"]]
        for names in invalid:
            with self.subTest(names=names), patch.object(Path, "lstat") as probe:
                with self.assertRaises(ValueError):
                    directory_stats(self.root, exclude_dirs=names)
                with self.assertRaises(ValueError):
                    inspect_app(InventoryResult(), "unknown", exclude_dirs=names)
                probe.assert_not_called()

    def test_exclusion_does_not_consume_file_limit(self):
        self.file("cache/data")
        kept = self.file("kept")
        with patch("storage_inspector.scanner.os.scandir",
                   return_value=iter([self.root / "cache", kept])):
            result = directory_stats(self.root, exclude_dirs=["cache"], max_files=1)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.excluded_directories, 1)
        self.assertEqual(result.stop_reason, "max_files")

    def test_interruption_retains_exclusion_count_and_closes_iterator(self):
        (self.root / "cache").mkdir()

        class Entries:
            closed = False

            def __init__(self, path):
                self.paths = iter([path])

            def __next__(self):
                try:
                    return next(self.paths)
                except StopIteration:
                    raise KeyboardInterrupt

            def close(self):
                self.closed = True

        entries = Entries(self.root / "cache")
        with patch("storage_inspector.scanner.os.scandir", return_value=entries):
            result = directory_stats(self.root, exclude_dirs=["cache"])
        self.assertEqual(result.excluded_directories, 1)
        self.assertEqual(result.stop_reason, "interrupted")
        self.assertTrue(entries.closed)

    def test_cli_repeated_filters_and_partial_exit(self):
        self.file("cache/data")
        self.file(".git/data")
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--path", str(self.root), "--exclude-dir", "cache", "--exclude-dir", ".git"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue(report["partial"])
        self.assertEqual(report["exclude_dirs"], [".git", "cache"])
        self.assertEqual(report["excluded_directories"], 2)

    def test_cli_bad_options_fail_before_discovery(self):
        for args in [["--list-apps", "--exclude-dir", "cache"],
                     ["--app", "sample", "--exclude-dir", "a/b"]]:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), \
                    patch("storage_inspector.__main__.discover_inventory") as discover:
                with self.assertRaises(SystemExit) as raised:
                    main(args)
                self.assertEqual(raised.exception.code, 2)
                discover.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows installation paths")
    def test_cli_app_propagates_exclusions(self):
        self.file("cache/data")
        self.file("kept")
        inventory = InventoryResult(applications=[
            InstalledApp("sample", "Sample", install_location=str(self.root)),
        ])
        output = io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), redirect_stdout(output):
            status = main(["--app", "sample", "--exclude-dir", "CACHE", "--top-files", "1"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue(report["partial"])
        self.assertEqual(report["scan"]["file_count"], 1)
        self.assertEqual(report["scan"]["exclude_dirs"], ["cache"])
        self.assertEqual(report["scan"]["excluded_directories"], 1)


if __name__ == "__main__":
    unittest.main()
