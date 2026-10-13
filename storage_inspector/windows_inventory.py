from __future__ import annotations

import hashlib
import json
import ntpath
import os
from dataclasses import dataclass, field

from .domain import InstalledApp


@dataclass
class InventoryResult:
    applications: list[InstalledApp] = field(default_factory=list)
    error_count: int = 0
    errors: list[dict] = field(default_factory=list)
    missing_locations: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.error_count > 0

    def record_error(self, location: str, operation: str, error: OSError) -> None:
        self.error_count += 1
        if len(self.errors) < 100:
            self.errors.append({"location": location, "operation": operation,
                                "message": str(error), "winerror": getattr(error, "winerror", None)})


def _stable_id(*identity: str) -> str:
    encoded = json.dumps(identity, ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:24]
    return f"registry.{digest}"


def discover_installed_apps() -> list[InstalledApp]:
    """Compatibility list API; use discover_inventory for coverage diagnostics."""
    return discover_inventory().applications


def discover_inventory() -> InventoryResult:
    result = InventoryResult()
    if os.name != "nt":
        return result

    import winreg

    locations = [
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, "HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    discovered: dict[tuple[str, ...], InstalledApp] = {}

    for hive, hive_name, location in locations:
        root_location = f"{hive_name}\\{location}"
        try:
            root = winreg.OpenKey(hive, location)
        except FileNotFoundError:
            # An absent uninstall root is normal on some Windows installations.
            result.missing_locations.append(root_location)
            continue
        except OSError as error:
            result.record_error(root_location, "open_root", error)
            continue
        with root:
            try:
                count = winreg.QueryInfoKey(root)[0]
            except OSError as error:
                result.record_error(root_location, "query_root", error)
                continue
            for index in range(count):
                entry_location = f"{root_location} [index={index}]"
                try:
                    subkey_name = winreg.EnumKey(root, index)
                    entry_location = f"{root_location}\\{subkey_name}"
                    with winreg.OpenKey(root, subkey_name) as subkey:
                        values = _read_values(subkey, result, entry_location)
                except OSError as error:
                    result.record_error(entry_location, "read_entry", error)
                    continue
                name = str(values.get("DisplayName", "")).strip()
                if not name or values.get("SystemComponent") == 1:
                    continue
                publisher = str(values.get("Publisher", "")).strip()
                version = str(values.get("DisplayVersion", "")).strip()
                install_location = str(values.get("InstallLocation", "")).strip().strip('"')
                normalized_path = ntpath.normcase(ntpath.normpath(install_location)) if install_location else ""
                # Unknown paths must not swallow known installations or each other.
                # Version/path changes intentionally result in a different identity.
                key = (name.casefold(), publisher.casefold(), version, normalized_path,
                       "" if normalized_path else entry_location.casefold())
                candidate = InstalledApp(
                    app_id=_stable_id(*key),
                    name=name,
                    publisher=publisher,
                    version=version,
                    install_location=install_location,
                    uninstall_command=str(values.get("UninstallString", "")),
                )
                previous = discovered.get(key)
                if previous is None or (not previous.uninstall_command and candidate.uninstall_command):
                    discovered[key] = candidate

    result.applications = sorted(discovered.values(), key=lambda app: (
        app.name.casefold(), app.version, ntpath.normcase(app.install_location), app.app_id))
    return result


def _read_values(key, result: InventoryResult | None = None, location: str = "") -> dict[str, object]:
    import winreg

    values: dict[str, object] = {}
    for index in range(winreg.QueryInfoKey(key)[1]):
        try:
            name, value, _ = winreg.EnumValue(key, index)
            values[name] = value
        except OSError as error:
            if result is not None:
                result.record_error(location, f"read_value[{index}]", error)
            continue
    return values
