"""Polite fetching: robots.txt, per-host delay, size limits."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

from .. import http
from ..config import get_settings
from . import cache

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
    """Downloads pages and files for a crawl.

    ``use_saved`` answers from copies saved by an earlier crawl when there is
    one, without contacting the site. ``save`` keeps a copy of everything
    fetched (the ``FLOORED_CRAWL_CACHE`` setting decides the defaults).
    """

    def __init__(self, delay_s: float | None = None, *, use_saved: bool | None = None, save: bool | None = None) -> None:
        settings = get_settings()
        self.delay_s = settings.crawl_delay_s if delay_s is None else delay_s
        self.use_saved = settings.crawl_cache == "reuse" if use_saved is None else use_saved
        self.save = settings.crawl_cache != "off" if save is None else save
        self.from_saved = 0
        self.downloaded = 0
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
        if self.use_saved:
            hit = cache.load(url)
            if hit is not None:
                self.from_saved += 1
                return Response(hit.url, hit.status, hit.content_type, hit.content)
        resp = self._download(url)
        if resp is not None:
            self.downloaded += 1
            if self.save:
                cache.save(url, resp.url, resp.status, resp.content_type, resp.content)
        return resp

    def _download(self, url: str) -> Response | None:
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
