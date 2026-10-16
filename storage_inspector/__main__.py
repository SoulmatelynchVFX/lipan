"""Explicit, read-only entry points; no automatic disk scan or report persistence."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from .app_scan import inspect_app
from .scanner import directory_stats, validate_limits
from .windows_inventory import discover_inventory


def _main(argv=None):
    parser = argparse.ArgumentParser(description="璃盘：只读应用清单与指定路径统计")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-apps", action="store_true", help="读取 Windows 卸载注册表，不扫描目录")
    action.add_argument("--path", type=Path, help="只统计指定文件或目录的逻辑字节数")
    action.add_argument("--app", metavar="APP_ID", help="按清单 ID 只统计该应用声明的安装目录")
    parser.add_argument("--max-files", type=int, help="扫描至多计入指定数量的文件，必须为正整数")
    parser.add_argument("--max-seconds", type=float, help="扫描时长软限制（秒），必须为有限正数")
    args = parser.parse_args(argv)
    try:
        validate_limits(args.max_files, args.max_seconds)
    except ValueError as error:
        parser.error(str(error))
    if args.list_apps and (args.max_files is not None or args.max_seconds is not None):
        parser.error("扫描限制仅用于 --path 或 --app，不能用于 --list-apps")
    if args.list_apps or args.app is not None:
        if os.name != "nt":
            parser.error("--list-apps 和 --app 仅支持 Windows")
        inventory = discover_inventory()
    if args.app is not None:
        report = inspect_app(inventory, args.app, max_files=args.max_files, max_seconds=args.max_seconds)
        scan = report.get("scan") or {}
        status = 130 if scan.get("stop_reason") == "interrupted" else 2 if report["partial"] else 0
    elif args.list_apps:
        report = {
            **asdict(inventory),
            "source": "Windows uninstall registry",
            "partial": inventory.partial,
            "note": "注册表清单可能不完整；安装位置不代表应用全部数据，没有执行卸载命令。",
        }
        status = 2 if inventory.partial else 0
    else:
        stats = directory_stats(args.path, max_files=args.max_files, max_seconds=args.max_seconds)
        report = {**asdict(stats), "partial": stats.partial,
                  "note": "逻辑长度不是磁盘实际分配量或可清理容量；扫描期间变化可能造成误差。"}
        status = 130 if stats.stop_reason == "interrupted" else 2 if stats.partial else 0
    # ASCII escaping also makes redirected output usable with legacy Windows encodings.
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return status


def main(argv=None):
    try:
        return _main(argv)
    except KeyboardInterrupt:
        # Inventory discovery and output may be interrupted before a report is ready.
        print("操作已中断，未输出完整报告。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
