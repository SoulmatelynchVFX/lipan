"""Application metadata read from the Windows uninstall registry."""
from dataclasses import dataclass


@dataclass(slots=True)
class InstalledApp:
    app_id: str
    name: str
    publisher: str = ""
    version: str = ""
    install_location: str = ""
    uninstall_command: str = ""
    source: str = "注册表"
