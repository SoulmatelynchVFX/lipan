"""Explicit, read-only entry points; no automatic disk scan or report persistence."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from .app_scan import inspect_app
from .scanner import directory_stats
from .windows_inventory import discover_inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description="璃盘：只读应用清单与指定路径统计")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-apps", action="store_true", help="读取 Windows 卸载注册表，不扫描目录")
    action.add_argument("--path", type=Path, help="只统计指定文件或目录的逻辑字节数")
    action.add_argument("--app", metavar="APP_ID", help="按清单 ID 只统计该应用声明的安装目录")
    args = parser.parse_args(argv)
    if args.list_apps or args.app is not None:
        if os.name != "nt":
            parser.error("--list-apps 和 --app 仅支持 Windows")
        inventory = discover_inventory()
    if args.app is not None:
        report = inspect_app(inventory, args.app)
        status = 2 if report["partial"] else 0
    elif args.list_apps:
        report = {
            **asdict(inventory),
            "source": "Windows uninstall registry",
            "partial": inventory.partial,
            "note": "注册表清单可能不完整；安装位置不代表应用全部数据，没有执行卸载命令。",
        }
        status = 2 if inventory.partial else 0
    else:
        stats = directory_stats(args.path)
        report = {**asdict(stats), "partial": stats.partial,
                  "note": "逻辑长度不是磁盘实际分配量或可清理容量；扫描期间变化可能造成误差。"}
        status = 2 if stats.partial else 0
    # ASCII escaping also makes redirected output usable with legacy Windows encodings.
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
