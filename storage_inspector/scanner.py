"""Read file metadata only; never follow discovered links or open file contents."""
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import stat
import time


# Reparse points include Windows junctions and many cloud placeholders.
SKIP_ATTRIBUTES = 0x400 | 0x1000 | 0x40000 | 0x400000
MAX_ERROR_DETAILS = 100


@dataclass
class DirectoryStats:
    path: str
    logical_bytes: int = 0
    file_count: int = 0
    skipped: int = 0
    duplicates: int = 0
    error_count: int = 0
    errors: list[dict] = field(default_factory=list)
    stop_reason: str | None = None

    @property
    def partial(self):
        return bool(self.error_count or self.skipped or self.stop_reason)

    def record_error(self, path, error):
        self.error_count += 1
        if len(self.errors) < MAX_ERROR_DETAILS:
            self.errors.append({"path": str(path), "message": str(error)})


def should_skip(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & SKIP_ATTRIBUTES
    )


def validate_limits(max_files=None, max_seconds=None):
    if max_files is not None and (type(max_files) is not int or max_files <= 0):
        raise ValueError("max_files 必须是正整数")
    if max_seconds is not None and (
        type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds <= 0
    ):
        raise ValueError("max_seconds 必须是有限正数")


def directory_stats(path: Path, *, max_files=None, max_seconds=None) -> DirectoryStats:
    validate_limits(max_files, max_seconds)
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    # abspath normalizes the supplied path without resolving symlinks.
    root = Path(os.path.abspath(path))
    result = DirectoryStats(str(root))
    # Keep only active directory iterators, rather than collecting a wide directory
    # before examining its files. Close every iterator on early termination too.
    stack = [(root, iter((root,)))]
    seen = set()
    try:
        while stack:
            if deadline is not None and time.monotonic() >= deadline:
                result.stop_reason = "max_seconds"
                break
            if max_files is not None and result.file_count >= max_files:
                result.stop_reason = "max_files"
                break
            parent, entries = stack[-1]
            try:
                entry = next(entries)
            except (StopIteration, OSError) as error:
                stack.pop()
                close = getattr(entries, "close", None)
                if close:
                    close()
                if isinstance(error, OSError):
                    result.record_error(parent, error)
                continue
            current = entry if isinstance(entry, Path) else Path(entry.path)
            try:
                info = current.lstat()
                if should_skip(info):
                    result.skipped += 1
                    continue
                if stat.S_ISDIR(info.st_mode):
                    stack.append((current, os.scandir(current)))
                elif stat.S_ISREG(info.st_mode):
                    identity = (info.st_dev, info.st_ino)
                    if info.st_ino and identity in seen:
                        result.duplicates += 1
                        continue
                    if info.st_ino:
                        seen.add(identity)
                    result.logical_bytes += info.st_size
                    result.file_count += 1
                else:
                    result.skipped += 1
            except OSError as error:
                result.record_error(current, error)
    finally:
        for _, entries in reversed(stack):
            close = getattr(entries, "close", None)
            if close:
                close()
    return result
