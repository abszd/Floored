"""Read a web page for floor plan links and the clues around them."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..geo.matching import find_addresses

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp")
DOC_EXT = (".pdf",)

_PLAN_WORDS = re.compile(r"floor\s*-?\s*plans?|floorplans?|\bfloor\b|\blevel\b|\bbasement\b|\blayout\b|\bfl\d|\bflr\b", re.I)
_STRONG_PLAN = re.compile(r"floor\s*-?\s*plans?|floorplans?|building\s*plans?", re.I)
_NAV_WORDS = re.compile(
    r"floor|plan|building|facilit|map|campus|space|room|level|architect|location|directory|hall|center", re.I
)
_SKIP_WORDS = re.compile(r"logo|icon|sprite|banner|avatar|social|facebook|twitter|instagram|linkedin|youtube|header|footer", re.I)

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def parse_level(text: str) -> tuple[float, str] | None:
    """Find a floor level in text: ("3rd Floor" -> (3, "3rd Floor"))."""
    if not text:
        return None
    t = text.replace("_", " ")
    patterns: list[tuple[str, object]] = [
        (r"\bsub-?basement\b", -2),
        (r"\b(?:lower\s+level|basement|cellar)\b", -1),
        (r"\b(?:ground|main|street)\s+(?:floor|level)\b", 1),
        (r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+(?:floor|level)\b", "ord"),
        (r"\b(\d{1,3})(?:st|nd|rd|th)\s*(?:floor|level|fl)\b", "num"),
        (r"\b(?:floor|level|flr|fl|lvl)\s*[-#:.]?\s*(\d{1,3})\b", "num"),
        (r"\b(\d{1,3})\s*(?:floor|level)\b", "num"),
        (r"\b(?:floor|level)\s*[-#:.]?\s*(b\d|g|l|m|lg|ll)\b", "code"),
        (r"(?:^|[^a-z0-9])(?:fl|flr|lvl|level|floor)[-_ ]?(\d{1,2})(?:[^a-z0-9]|$)", "num"),
    ]
    for pat, kind in patterns:
        m = re.search(pat, t, re.I)
        if not m:
            continue
        label = re.sub(r"\s+", " ", m.group(0)).strip(" -_#:.").title()
        if isinstance(kind, int):
            return float(kind), label
        if kind == "ord":
            return float(_ORDINALS[m.group(1).lower()]), label
        if kind == "num":
            return float(int(m.group(1))), label
        code = m.group(1).lower()
        if code.startswith("b"):
            return -float(code[1:] or 1), label
        return (0.0 if code in ("lg", "ll") else 1.0), label
    return None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class AssetLink:
    url: str
    kind: str  # "image" | "pdf"
    text: str  # link text / alt / title
    context: str  # nearby headings and text
    score: float
    building_guess: str | None = None
    address_guess: str | None = None
    level: tuple[float, str] | None = None


@dataclass
class PageInfo:
    url: str
    title: str
    links: list[tuple[str, float]] = field(default_factory=list)  # (url, priority)
    assets: list[AssetLink] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)


def _nearest_heading(el: Tag) -> str:
    for prev in el.find_all_previous(["h1", "h2", "h3", "h4", "caption", "dt", "strong"], limit=6):
        text = _clean(prev.get_text(" "))
        if 2 < len(text) < 120:
            return text
    return ""


def _context(el: Tag) -> str:
    parent = el
    for _ in range(3):
        if parent.parent is None:
            break
        parent = parent.parent
        if len(_clean(parent.get_text(" "))) > 80:
            break
    return _clean(parent.get_text(" "))[:600]


def _building_guess(title: str, heading: str, text: str) -> str | None:
    """Pick the most specific text that names a building rather than a floor."""
    from ..geo.matching import _NOISE, normalize_name

    generic = re.compile(
        r"(download|view|click here|here|open|pdf|image|print|full size|larger|enlarge|"
        r"first|second|third|fourth|fifth|ground|lower|upper|main|north|south|east|west|\d+|[a-z])",
    )
    for candidate in (text, heading, re.split(r"\s[|–—-]\s", title)[0]):
        candidate = _clean(candidate)
        stripped = normalize_name(candidate)
        if stripped and len(stripped) >= 3 and not all(generic.fullmatch(w) for w in stripped.split()):
            cleaned = _clean(_NOISE.sub(" ", candidate)).strip(" -–—,:|")
            return (cleaned or candidate)[:200]
    return None


def _same_site(url: str, domains: list[str]) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


def parse_page(url: str, html: str, allowed_domains: list[str]) -> PageInfo:
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "noscript", "nav", "footer"]):
        # Navigation menus mention every building; keep their links but not their text.
        if bad.name in ("nav", "footer"):
            continue
        bad.decompose()
    title = _clean(soup.title.get_text()) if soup.title else ""
    info = PageInfo(url=url, title=title, addresses=find_addresses(soup.get_text(" ")))
    seen: set[str] = set()

    def add_asset(href: str, kind: str, el: Tag, text: str) -> None:
        if href in seen or _SKIP_WORDS.search(href):
            return
        seen.add(href)
        heading = _nearest_heading(el)
        ctx = _context(el)
        blob = " ".join([href.rsplit("/", 1)[-1], text, heading, title])
        score = 0.0
        if _STRONG_PLAN.search(blob):
            score += 0.6
        elif _PLAN_WORDS.search(blob):
            score += 0.35
        if kind == "pdf":
            score += 0.1
        level = parse_level(text) or parse_level(href.rsplit("/", 1)[-1]) or parse_level(heading)
        if level:
            score += 0.2
        local_addr = find_addresses(ctx)
        info.assets.append(
            AssetLink(
                url=href,
                kind=kind,
                text=text,
                context=_clean(f"{heading} — {ctx}")[:600],
                score=min(score, 1.0),
                building_guess=_building_guess(title, heading, text),
                address_guess=(local_addr or info.addresses or [None])[0],
                level=level,
            )
        )

    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(url, a["href"]))[0]
        if not href.startswith(("http://", "https://")):
            continue
        text = _clean(a.get_text(" ")) or _clean(a.get("title"))
        path = urlsplit(href).path.lower()
        if path.endswith(DOC_EXT):
            add_asset(href, "pdf", a, text)
        elif path.endswith(IMAGE_EXT):
            add_asset(href, "image", a, text)
        elif _same_site(href, allowed_domains):
            blob = f"{text} {path}"
            prio = 1.0 if _STRONG_PLAN.search(blob) else 0.6 if _NAV_WORDS.search(blob) else 0.1
            info.links.append((href, prio))

    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src")
        if not src:
            continue
        href = urldefrag(urljoin(url, src))[0]
        if not href.startswith(("http://", "https://")):
            continue
        # Thumbnails that link to a full-size image are handled via the <a>.
        if img.find_parent("a", href=True) and urlsplit(urljoin(url, img.find_parent("a")["href"])).path.lower().endswith(IMAGE_EXT + DOC_EXT):
            continue
        text = _clean(" ".join(filter(None, [img.get("alt"), img.get("title")])))
        add_asset(href, "image", img, text)

    return info
