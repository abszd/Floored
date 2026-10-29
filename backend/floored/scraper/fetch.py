"""Polite fetching: robots.txt, per-host delay, size limits."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

from .. import http
from ..config import get_settings

MAX_BYTES = 40 * 1024 * 1024


@dataclass
class Response:
    url: str
    status: int
    content_type: str
    content: bytes

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, delay_s: float | None = None) -> None:
        self.delay_s = get_settings().crawl_delay_s if delay_s is None else delay_s
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if origin in self._robots:
                return self._robots[origin]
        parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
        try:
            resp = http.client().get(f"{origin}/robots.txt", timeout=10)
            if resp.status_code >= 400:
                parser = None  # no robots.txt: everything allowed
            else:
                parser.parse(resp.text.splitlines())
        except httpx.HTTPError:
            parser = None
        with self._lock:
            self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(get_settings().user_agent, url)

    def get(self, url: str) -> Response | None:
        if not self.allowed(url):
            return None
        rp = self._robots_for(url)
        delay = max(self.delay_s, float(rp.crawl_delay(get_settings().user_agent) or 0) if rp else 0)
        http.throttle(url, delay)
        try:
            with http.client().stream("GET", url) as resp:
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        return None
                    chunks.append(chunk)
                return Response(str(resp.url), resp.status_code, ctype, b"".join(chunks))
        except httpx.HTTPError:
            return None
