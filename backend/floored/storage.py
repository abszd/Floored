"""Local media storage for floor plan images and debug renders."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import get_settings


def save_bytes(data: bytes, folder: str, suffix: str) -> str:
    """Store ``data`` content-addressed under ``media/<folder>`` and return its relative path."""
    digest = hashlib.sha256(data).hexdigest()[:20]
    rel = Path(folder) / f"{digest}{suffix}"
    path = get_settings().media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return rel.as_posix()


def media_path(rel: str) -> Path:
    root = get_settings().media_dir.resolve()
    path = (root / rel).resolve()
    if root not in path.parents and path != root:
        raise ValueError("path escapes media directory")
    return path


def read_bytes(rel: str) -> bytes:
    return media_path(rel).read_bytes()
