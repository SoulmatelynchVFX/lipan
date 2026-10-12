from __future__ import annotations

import hashlib
import os

from .domain import InstalledApp


def _stable_id(name: str, publisher: str) -> str:
    digest = hashlib.sha1(f"{publisher}|{name}".encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"registry.{digest}"


def discover_installed_apps() -> list[InstalledApp]:
    if os.name != "nt":
        return []

    import winreg

    locations = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    discovered: dict[tuple[str, str], InstalledApp] = {}

    for hive, location in locations:
        try:
            root = winreg.OpenKey(hive, location)
        except OSError:
            continue
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    subkey_name = winreg.EnumKey(root, index)
                    with winreg.OpenKey(root, subkey_name) as subkey:
                        values = _read_values(subkey)
                except OSError:
                    continue
                name = str(values.get("DisplayName", "")).strip()
                if not name or values.get("SystemComponent") == 1:
                    continue
                publisher = str(values.get("Publisher", "")).strip()
                key = (name.casefold(), publisher.casefold())
                candidate = InstalledApp(
                    app_id=_stable_id(name, publisher),
                    name=name,
                    publisher=publisher,
                    version=str(values.get("DisplayVersion", "")),
                    install_location=str(values.get("InstallLocation", "")).strip().strip('"'),
                    uninstall_command=str(values.get("UninstallString", "")),
                )
                previous = discovered.get(key)
                if previous is None or (not previous.install_location and candidate.install_location):
                    discovered[key] = candidate

    return sorted(discovered.values(), key=lambda app: app.name.casefold())


def _read_values(key) -> dict[str, object]:
    import winreg

    values: dict[str, object] = {}
    for index in range(winreg.QueryInfoKey(key)[1]):
        try:
            name, value, _ = winreg.EnumValue(key, index)
            values[name] = value
        except OSError:
            continue
    return values
