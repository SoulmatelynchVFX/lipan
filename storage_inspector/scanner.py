"""Read file metadata only; never follow discovered links or open file contents."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat


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

    @property
    def partial(self):
        return bool(self.error_count or self.skipped)

    def record_error(self, path, error):
        self.error_count += 1
        if len(self.errors) < MAX_ERROR_DETAILS:
            self.errors.append({"path": str(path), "message": str(error)})


def should_skip(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & SKIP_ATTRIBUTES
    )


def directory_stats(path: Path) -> DirectoryStats:
    # abspath normalizes the supplied path without resolving symlinks.
    root = Path(os.path.abspath(path))
    result = DirectoryStats(str(root))
    stack = [root]
    seen = set()
    while stack:
        current = stack.pop()
        try:
            info = current.lstat()
            if should_skip(info):
                result.skipped += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as entries:
                    for entry in entries:
                        stack.append(Path(entry.path))
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
    return result
