"""Durable blob storage for encrypted chunks.

Every write is: temp file -> fsync(file) -> rename -> fsync(dir). Only after
that does the caller commit the database row, so a crash can leave an orphan
blob (harmless, reported by the admin page) but never a database row that
points at data which is not on disk.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import settings


def session_dir(session_id: str, root: Path | None = None) -> Path:
    base = root or settings.blob_dir
    # Two-level fan-out keeps directory sizes reasonable on large stores.
    return base / session_id[:2] / session_id


def chunk_path(session_id: str, sequence: int, root: Path | None = None) -> Path:
    return session_dir(session_id, root) / f"chunk-{sequence:06d}.flac.enc"


def plaintext_path(session_id: str, sequence: int, root: Path | None = None) -> Path:
    return session_dir(session_id, root) / f"chunk-{sequence:06d}.flac"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        written = 0
        view = memoryview(data)
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_blob(path: Path) -> bytes:
    return Path(path).read_bytes()


def delete_blob(path: Path | str) -> bool:
    p = Path(path)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def delete_session_blobs(session_id: str, root: Path | None = None) -> int:
    directory = session_dir(session_id, root)
    if not directory.exists():
        return 0
    count = sum(1 for _ in directory.rglob("*") if _.is_file())
    shutil.rmtree(directory, ignore_errors=True)
    return count


def disk_usage(root: Path | None = None) -> tuple[int, int, int]:
    """(total, used, free) bytes for the data volume."""
    base = root or settings.data_dir
    try:
        stat = os.statvfs(base)
    except OSError:
        return (0, 0, 0)
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    return (total, total - free, free)


def blob_bytes(root: Path | None = None) -> int:
    base = root or settings.blob_dir
    if not base.exists():
        return 0
    total = 0
    for path in base.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total
