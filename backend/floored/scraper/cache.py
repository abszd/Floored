"""Saved copies of the pages and files a crawl downloads.

Every response is kept under ``<data>/cache/pages`` so a crawl can be run
again from disk, without sending the site a single request: handy when
testing changes to the crawler, matching or the converter against a real
site. Lookups and footprints from OpenStreetMap are cached separately (see
``floored.http``), so a re-run needs no network at all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..config import get_settings


@dataclass
class Saved:
    url: str
    status: int
    content_type: str
    content: bytes


def _root() -> Path:
    return get_settings().cache_dir / "pages"


def _paths(url: str) -> tuple[Path, Path]:
    key = hashlib.sha256(url.encode()).hexdigest()
    host = urlsplit(url).netloc.lower().replace(":", "_") or "_"
    folder = _root() / host / key[:2]
    return folder / f"{key}.json", folder / f"{key}.bin"


def load(url: str) -> Saved | None:
    meta_path, body_path = _paths(url)
    try:
        meta = json.loads(meta_path.read_text())
        body = body_path.read_bytes() if meta.get("has_body") else b""
    except (OSError, ValueError):
        return None
    return Saved(meta["url"], int(meta["status"]), meta.get("content_type", ""), body)


def save(url: str, final_url: str, status: int, content_type: str, content: bytes) -> None:
    meta_path, body_path = _paths(url)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    keep_body = status < 400
    if keep_body:
        body_path.write_bytes(content)
    meta_path.write_text(
        json.dumps({"url": final_url, "requested": url, "status": status, "content_type": content_type, "has_body": keep_body})
    )


def stats(domains: list[str] | None = None) -> dict[str, int]:
    """How many responses are saved (for these hosts), and their size in bytes."""
    root = _root()
    if not root.exists():
        return {"responses": 0, "bytes": 0}
    n = size = 0
    for h in _hosts(domains):
        for p in h.rglob("*.json"):
            n += 1
            size += p.stat().st_size
            b = p.with_suffix(".bin")
            if b.exists():
                size += b.stat().st_size
    return {"responses": n, "bytes": size}


def clear(domains: list[str] | None = None) -> None:
    for h in _hosts(domains):
        shutil.rmtree(h, ignore_errors=True)


def _hosts(domains: list[str] | None) -> list[Path]:
    """Saved hosts, limited to these domains and their subdomains."""
    root = _root()
    if not root.exists():
        return []
    hosts = [p for p in root.iterdir() if p.is_dir()]
    if not domains:
        return hosts
    want = [d.lower().lstrip(".").split(":")[0] for d in domains]
    return [h for h in hosts if any(h.name == d or h.name.endswith("." + d) for d in want)]
