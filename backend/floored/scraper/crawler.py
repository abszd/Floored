"""Crawl a site for floor plans.

Pages are visited best-first: links whose text or URL mentions floor plans
are followed before generic ones, so a capped crawl spends its budget where
plans are likely to be. Every image or PDF linked from a visited page is
scored from its surrounding text, and the promising ones are downloaded and
checked with an image heuristic.
"""

from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator
from urllib.parse import urlsplit

from . import classify, documents
from .extract import DOC_EXT, IMAGE_EXT, AssetLink, parse_level, parse_page
from .fetch import Fetcher

# Text score an asset needs before we spend a download on it.
MIN_TEXT_SCORE = 0.3
# Image score a downloaded page needs to be kept.
MIN_PLAN_SCORE = 0.45


@dataclass
class FoundPlan:
    asset_url: str
    page_index: int
    page_url: str
    page_title: str
    context: str
    png: bytes
    plan_score: float
    building_guess: str | None
    address_guess: str | None
    level: float | None
    level_label: str | None


@dataclass
class CrawlConfig:
    seed_urls: list[str]
    allowed_domains: list[str]
    max_pages: int = 200
    max_depth: int = 3
    max_assets: int = 300


def _domains(cfg: CrawlConfig) -> list[str]:
    if cfg.allowed_domains:
        return [d.lower().lstrip(".") for d in cfg.allowed_domains]
    return sorted({urlsplit(u).netloc.lower().split(":")[0] for u in cfg.seed_urls})


def crawl(
    cfg: CrawlConfig,
    *,
    fetcher: Fetcher | None = None,
    skip_assets: Iterable[str] = (),
    progress: Callable[[float, str], None] | None = None,
) -> Iterator[FoundPlan]:
    fetcher = fetcher or Fetcher()
    domains = _domains(cfg)
    skip = set(skip_assets)
    seen_pages: set[str] = set()
    seen_hashes: set[str] = set()
    queue: list[tuple[float, int, int, str]] = []
    counter = 0
    for url in cfg.seed_urls:
        heapq.heappush(queue, (-2.0, 0, counter, url))
        counter += 1
    assets: list[tuple[AssetLink, str, str]] = []
    pages = 0

    def report(msg: str) -> None:
        if progress:
            done = min(pages / max(cfg.max_pages, 1), 1.0)
            progress(0.6 * done, msg)

    while queue and pages < cfg.max_pages:
        _, depth, _, url = heapq.heappop(queue)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        path = urlsplit(url).path.lower()
        if path.endswith(IMAGE_EXT + DOC_EXT):
            # A seed can point straight at a plan.
            level = parse_level(path.rsplit("/", 1)[-1])
            assets.append(
                (AssetLink(url, "pdf" if path.endswith(DOC_EXT) else "image", "", "", 1.0, level=level), url, "")
            )
            continue
        resp = fetcher.get(url)
        pages += 1
        if resp is None or resp.status >= 400 or not resp.is_html:
            continue
        info = parse_page(resp.url, resp.text, domains)
        report(f"Read {pages} pages, {len(assets)} possible plans: {info.title or url}")
        for a in info.assets:
            if a.score >= MIN_TEXT_SCORE and a.url not in skip:
                assets.append((a, info.url, info.title))
        if depth < cfg.max_depth:
            for link, prio in info.links:
                if link not in seen_pages:
                    heapq.heappush(queue, (-prio + 0.1 * depth, depth + 1, counter, link))
                    counter += 1

    # Best candidates first, one download each.
    unique: dict[str, tuple[AssetLink, str, str]] = {}
    for a, page_url, title in assets:
        if a.url not in unique or a.score > unique[a.url][0].score:
            unique[a.url] = (a, page_url, title)
    ranked = sorted(unique.values(), key=lambda t: -t[0].score)[: cfg.max_assets]

    for i, (a, page_url, title) in enumerate(ranked):
        if progress:
            progress(0.6 + 0.4 * i / max(len(ranked), 1), f"Checking {a.url.rsplit('/', 1)[-1]}")
        resp = fetcher.get(a.url)
        if resp is None or resp.status >= 400:
            continue
        try:
            pages_imgs = documents.load(resp.content, resp.content_type, a.url)
        except Exception:
            continue
        for page in pages_imgs:
            digest = hashlib.sha256(page.png).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            score = classify.plan_likeness(page.bgr)
            if score < MIN_PLAN_SCORE:
                continue
            # Multi-page PDFs usually have one floor per page, named in the page text.
            level = parse_level(page.text) if page.text else None
            level = level or a.level
            yield FoundPlan(
                asset_url=a.url,
                page_index=page.index,
                page_url=page_url,
                page_title=title,
                context=(a.context + (" | " + page.text[:300] if page.text else ""))[:900],
                png=page.png,
                plan_score=round(0.5 * score + 0.5 * a.score, 3),
                building_guess=a.building_guess,
                address_guess=a.address_guess,
                level=level[0] if level else None,
                level_label=level[1] if level else None,
            )
