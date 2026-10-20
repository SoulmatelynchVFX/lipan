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

from storage_inspector.__main__ import main
from storage_inspector.domain import InstalledApp
from storage_inspector.scanner import directory_stats, should_skip, skip_reason
from storage_inspector.windows_inventory import InventoryResult


def info(mode=stat.S_IFREG, attributes=0):
    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_size=3, st_dev=1, st_ino=0)


class SkipDetailsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def assert_counts(self, result):
        self.assertEqual(sum(result.skip_reason_counts.values()), result.skipped)
        self.assertEqual(len(result.skip_details) + result.skip_details_omitted, result.skipped)

    def test_classification_and_existing_boolean_helper(self):
        for metadata, reason in [
            (info(stat.S_IFLNK), "symbolic_link"),
            (info(attributes=0x400), "reparse_point"),
            (info(attributes=0x1000), "offline"),
            (info(attributes=0x40000), "recall_on_open"),
            (info(attributes=0x400000), "recall_on_data_access"),
            (info(), None), (info(stat.S_IFDIR), None),
            (SimpleNamespace(st_mode=stat.S_IFREG), None),
        ]:
            with self.subTest(reason=reason):
                self.assertEqual(skip_reason(metadata), reason)
                self.assertEqual(should_skip(metadata), reason is not None)

    def test_primary_reason_precedence(self):
        for mode, flags, reason in [
            (stat.S_IFLNK, 0x441400, "symbolic_link"),
            (stat.S_IFDIR, 0x441400, "reparse_point"),
            (stat.S_IFREG, 0x441000, "offline"),
            (stat.S_IFREG, 0x440000, "recall_on_open"),
        ]:
            with self.subTest(reason=reason):
                self.assertEqual(skip_reason(info(mode, flags)), reason)

    def test_skipped_root_does_not_enumerate_or_open_contents(self):
        for metadata, reason in [(info(stat.S_IFLNK), "symbolic_link"),
                                 (info(stat.S_IFDIR, 0x400), "reparse_point"),
                                 (info(attributes=0x1000), "offline")]:
            with self.subTest(reason=reason), patch.object(Path, "lstat", return_value=metadata), \
                    patch("storage_inspector.scanner.os.scandir") as scan, patch.object(Path, "open") as opened:
                result = directory_stats(self.root, top_files=5, by_directory=True, by_extension=True)
            scan.assert_not_called()
            opened.assert_not_called()
            self.assertEqual(result.skip_details, [{"path": str(self.root), "reason": reason}])
            self.assertEqual(result.skip_reason_counts, {reason: 1})
            self.assertEqual((result.logical_bytes, result.file_count), (0, 0))
            self.assertEqual((result.largest_files, result.directory_summary, result.extension_summary), ([], [], []))
            self.assertTrue(result.partial)
            self.assert_counts(result)

    def test_special_file_types_have_explicit_reason(self):
        for mode in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(mode=mode), patch.object(Path, "lstat", return_value=info(mode)):
                result = directory_stats(self.root)
            self.assertEqual(result.skip_details, [{"path": str(self.root), "reason": "special_file"}])
            self.assert_counts(result)

    def test_detail_cap_keeps_all_counts_and_separate_error_budget(self):
        paths = [self.root / str(index) for index in range(105)]
        metadata = {self.root: info(stat.S_IFDIR), **{path: info(attributes=0x1000) for path in paths}}
        with patch.object(Path, "lstat", autospec=True, side_effect=lambda path: metadata[path]), \
                patch("storage_inspector.scanner.os.scandir", return_value=iter(paths)):
            result = directory_stats(self.root)
        self.assertEqual(result.skip_reason_counts, {"offline": 105})
        self.assertEqual(len(result.skip_details), 100)
        self.assertEqual(result.skip_details_omitted, 5)
        self.assertEqual(result.skip_details[-1]["path"], str(paths[99]))
        for _ in range(105):
            result.record_error(self.root, PermissionError("denied"))
        self.assertEqual((result.error_count, len(result.errors)), (105, 100))
        self.assert_counts(result)

    def test_errors_exclusions_and_duplicates_are_not_skip_details(self):
        excluded = self.root / "cache"
        excluded.mkdir()
        original = self.root / "original"
        original.write_bytes(b"abc")
        alias = self.root / "alias"
        os.link(original, alias)
        with patch("storage_inspector.scanner.os.scandir",
                   return_value=iter([excluded, original, alias, self.root / "missing"])):
            result = directory_stats(self.root, exclude_dirs=["cache"])
        self.assertEqual((result.excluded_directories, result.duplicates, result.error_count), (1, 1, 1))
        self.assertEqual(result.skip_reason_counts, {})
        self.assertEqual(result.skip_details, [])
        self.assert_counts(result)

    def test_cli_skip_details_and_exit_code(self):
        output = io.StringIO()
        with patch.object(Path, "lstat", return_value=info(attributes=0x400000)), redirect_stdout(output):
            status = main(["--path", str(self.root)])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue(report["partial"])
        self.assertEqual(report["skip_reason_counts"], {"recall_on_data_access": 1})
        self.assertEqual(report["skip_details"], [{"path": str(self.root), "reason": "recall_on_data_access"}])
        self.assertEqual(report["skip_details_omitted"], 0)

    def test_interruption_retains_details_and_closes_iterator(self):
        skipped = self.root / "offline"
        metadata = {self.root: info(stat.S_IFDIR), skipped: info(attributes=0x1000)}

        class Entries:
            closed = False

            def __init__(self):
                self.paths = iter([skipped])

            def __next__(self):
                try:
                    return next(self.paths)
                except StopIteration:
                    raise KeyboardInterrupt

            def close(self):
                self.closed = True

        entries = Entries()
        with patch.object(Path, "lstat", autospec=True, side_effect=lambda path: metadata[path]), \
                patch("storage_inspector.scanner.os.scandir", return_value=entries):
            result = directory_stats(self.root)
        self.assertEqual(result.stop_reason, "interrupted")
        self.assertEqual(result.skip_details, [{"path": str(skipped), "reason": "offline"}])
        self.assertTrue(entries.closed)
        self.assert_counts(result)

    def test_limits_retain_only_observed_details(self):
        skipped, kept = self.root / "offline", self.root / "kept"
        metadata = {self.root: info(stat.S_IFDIR), skipped: info(attributes=0x1000), kept: info()}
        with patch.object(Path, "lstat", autospec=True, side_effect=lambda path: metadata[path]), \
                patch("storage_inspector.scanner.os.scandir", return_value=iter([skipped, kept])):
            result = directory_stats(self.root, max_files=1)
        self.assertEqual(result.stop_reason, "max_files")
        self.assertEqual((result.skipped, result.file_count), (1, 1))
        self.assert_counts(result)
        with patch.object(Path, "lstat", autospec=True, side_effect=lambda path: metadata[path]), \
                patch("storage_inspector.scanner.os.scandir", return_value=iter([skipped, kept])), \
                patch("storage_inspector.scanner.time.monotonic", side_effect=[0, 0, 0, 2]):
            result = directory_stats(self.root, max_seconds=1)
        self.assertEqual(result.stop_reason, "max_seconds")
        self.assertEqual((result.skipped, result.file_count), (1, 0))
        self.assert_counts(result)

    @unittest.skipUnless(os.name == "nt", "Windows installation paths")
    def test_cli_app_exposes_scan_details(self):
        skipped = self.root / "offline"
        metadata = {self.root: info(stat.S_IFDIR), skipped: info(attributes=0x1000)}
        inventory = InventoryResult(applications=[InstalledApp("sample", "Sample", install_location=str(self.root))])
        output = io.StringIO()
        with patch("storage_inspector.__main__.discover_inventory", return_value=inventory), \
                patch.object(Path, "lstat", autospec=True, side_effect=lambda path: metadata[path]), \
                patch("storage_inspector.scanner.os.scandir", return_value=iter([skipped])), redirect_stdout(output):
            status = main(["--app", "sample"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue(report["partial"])
        self.assertEqual(report["scan"]["skip_details"], [{"path": str(skipped), "reason": "offline"}])


if __name__ == "__main__":
    unittest.main()
