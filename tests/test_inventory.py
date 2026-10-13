"""Synthetic registry coverage; no access to installed software or user files."""
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from storage_inspector.__main__ import main
from storage_inspector.windows_inventory import discover_inventory


ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


class Key:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Registry:
    HKEY_LOCAL_MACHINE = 1
    HKEY_CURRENT_USER = 2

    def __init__(self, records, failure=None):
        self.records = records
        self.failure = failure

    def OpenKey(self, parent, name):
        if isinstance(parent, Key):
            if self.failure == "entry":
                raise PermissionError("entry denied")
            return Key(self.records[name])
        if parent != 1 or name != ROOT:
            raise FileNotFoundError("absent root")
        if self.failure == "root":
            raise PermissionError("root denied")
        return Key(None)

    def QueryInfoKey(self, key):
        if key.value is None:
            if self.failure == "query_root":
                raise PermissionError("root query denied")
            return len(self.records), 0, 0
        if self.failure == "query_entry":
            raise PermissionError("entry query denied")
        return 0, len(key.value), 0

    def EnumKey(self, key, index):
        if self.failure == "enum":
            raise OSError("entry disappeared")
        return list(self.records)[index]

    def EnumValue(self, key, index):
        name, value = list(key.value.items())[index]
        if self.failure == "value" and name == "Publisher":
            raise PermissionError("value denied")
        return name, value, 1


def entry(version="1", path="D:\\Editor"):
    return {"DisplayName": "Editor", "Publisher": "Maker",
            "DisplayVersion": version, "InstallLocation": path}


class InventoryTests(unittest.TestCase):
    def read(self, records, failure=None):
        with patch.dict("sys.modules", {"winreg": Registry(records, failure)}), \
                patch("storage_inspector.windows_inventory.os.name", "nt"):
            return discover_inventory()

    def test_versions_and_install_locations_stay_separate(self):
        result = self.read({"a": entry(), "b": entry("2"), "c": entry("1", "E:\\Editor")})
        self.assertEqual(len(result.applications), 3)
        self.assertEqual(len({app.app_id for app in result.applications}), 3)
        self.assertFalse(result.partial)

    def test_identical_metadata_with_equivalent_paths_merges(self):
        result = self.read({"a": entry(), "b": entry(path="d:/editor/")})
        self.assertEqual(len(result.applications), 1)

    def test_missing_paths_remain_separate(self):
        result = self.read({"a": entry(path=""), "b": entry(path=""), "c": entry()})
        self.assertEqual(len(result.applications), 3)
        self.assertEqual(len({app.app_id for app in result.applications}), 3)

    def test_ids_do_not_depend_on_enumeration_order(self):
        records = {"a": entry(), "b": entry("2"), "unknown": entry(path="")}
        first = self.read(records)
        second = self.read(dict(reversed(list(records.items()))))
        self.assertEqual([app.app_id for app in first.applications],
                         [app.app_id for app in second.applications])

    def test_missing_roots_are_reported_without_false_access_errors(self):
        result = self.read({})
        self.assertEqual(len(result.missing_locations), 2)
        self.assertFalse(result.partial)
        self.assertEqual(result.error_count, 0)

    def test_failures_at_each_registry_stage_are_visible(self):
        for failure in ("root", "query_root", "enum", "entry", "query_entry", "value"):
            with self.subTest(failure=failure):
                result = self.read({"a": entry()}, failure)
                self.assertTrue(result.partial)
                self.assertEqual(result.error_count, 1)
                self.assertEqual(len(result.errors), 1)
                self.assertIn("HKLM", result.errors[0]["location"])

    def test_value_failure_keeps_readable_fields(self):
        result = self.read({"a": entry()}, "value")
        self.assertEqual(len(result.applications), 1)
        self.assertEqual(result.applications[0].name, "Editor")
        self.assertEqual(result.applications[0].publisher, "")

    def test_errors_are_bounded_without_losing_total(self):
        result = self.read({str(i): entry() for i in range(105)}, "entry")
        self.assertEqual(result.error_count, 105)
        self.assertEqual(len(result.errors), 100)

    def test_cli_exposes_diagnostics_and_exit_status(self):
        for failure, status in [(None, 0), ("root", 2)]:
            with self.subTest(failure=failure):
                output = io.StringIO()
                with patch.dict("sys.modules", {"winreg": Registry({"a": entry()}, failure)}), \
                        patch("storage_inspector.windows_inventory.os.name", "nt"), \
                        redirect_stdout(output):
                    self.assertEqual(main(["--list-apps"]), status)
                report = json.loads(output.getvalue())
                self.assertEqual(report["partial"], failure is not None)
                self.assertIn("missing_locations", report)
                self.assertIn("errors", report)


if __name__ == "__main__":
    unittest.main()
