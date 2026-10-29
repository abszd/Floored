"""Shared HTTP client: identifying User-Agent, per-host rate limits, JSON cache."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import get_settings

_lock = threading.Lock()
_last_hit: dict[str, float] = {}
_client: httpx.Client | None = None
_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Route all requests through ``transport`` (tests use ``httpx.MockTransport``)."""
    global _client, _transport
    _transport = transport
    if _client is not None:
        _client.close()
    _client = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={"User-Agent": get_settings().user_agent},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            transport=_transport,
        )
    return _client


def throttle(url: str, min_interval: float) -> None:
    """Block until at least ``min_interval`` seconds have passed since the last request to this host."""
    host = urlsplit(url).netloc
    while True:
        with _lock:
            now = time.monotonic()
            wait = _last_hit.get(host, 0.0) + min_interval - now
            if wait <= 0:
                _last_hit[host] = now
                return
        time.sleep(wait)


def get_json(url: str, params: dict[str, Any] | None = None, *, min_interval: float = 1.0, cache: bool = True) -> Any:
    key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    path = get_settings().cache_dir / "http" / f"{key}.json"
    if cache and path.exists():
        return json.loads(path.read_text())
    throttle(url, min_interval)
    resp = client().get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data


def post_form_json(url: str, data: dict[str, str], *, min_interval: float = 1.0) -> Any:
    key = hashlib.sha256(json.dumps([url, data], sort_keys=True).encode()).hexdigest()
    path = get_settings().cache_dir / "http" / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    throttle(url, min_interval)
    resp = client().post(url, data=data)
    resp.raise_for_status()
    out = resp.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    return out
