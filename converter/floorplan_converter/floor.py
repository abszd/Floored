"""Which floor a plan shows, read from the text on it.

Plans say which floor they are in a few ways, and all of them are read:

* a title or legend: "Second Floor Plan", "LEVEL 02", "Lower Level", "Basement",
  "Memorial Union - 3rd Floor";
* a big mark on its own beside the plan: "4", "B", "5R" (wayfinding maps);
* the room numbers: 4195, 4214 and (4304) are on floor 4, B104 is in the basement.

A title wins over a lone mark, and both are checked against the room numbers.

Levels count the way buildings are numbered: 1 is the first floor, 2 the second,
0 a floor called "ground", -1 and -2 basements (and "lower levels"), and a
mezzanine half a level above the floor it belongs to (1.5).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass

import cv2
import numpy as np

from . import text as text_mod
from .loading import load_image

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
}
_CARDINALS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_ORD = "|".join(_ORDINALS)
_CARD = "|".join(_CARDINALS)
_STOREY = r"(?:floor|level|story|storey|flr|lvl|fl)"
_SEP = r"\s*[-#:.]?\s*"
# Numbers OCR reads with letters in them ("LEVEL O2", "Floor l").
_DIGITS = r"[0-9oOlI]{1,2}"
# OCR runs title words together: "SECONDFLOORPLAN".
_END = r"(?:\b|(?=plans?\b))"

# (pattern, how to read it, strength). The first pattern that matches wins, so the
# specific ones come first. Strength is how sure the wording is of a floor.
_PHRASES = [
    (r"\bsub\s*-?\s*basement\b", "sub", 1.0),
    (rf"\b(?:basement|lower\s*level|cellar)\s*-?\s*({_DIGITS})\b", "basement_n", 1.0),
    (rf"\b(?:{_STOREY}){_SEP}b\s*-?\s*({_DIGITS})?\b", "basement_n", 1.0),
    (rf"\b(?:{_STOREY}){_SEP}(?:ll|lg)\s*({_DIGITS})?\b", "basement_n", 1.0),
    (rf"\b({_ORD})\s*(?:basement|lower\s*level)\b", "basement_ord", 1.0),
    (rf"\b(?:basement|lower\s*level|lower\s*ground|cellar|undercroft){_END}", -1.0, 1.0),
    (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*{_STOREY}\s*mezz(?:anine)?\b", "mezz_n", 1.0),
    (rf"\b({_ORD})\s*{_STOREY}\s*mezz(?:anine)?\b", "mezz_ord", 1.0),
    (rf"\bmezz(?:anine)?\s*(?:{_STOREY})?{_SEP}(\d{{1,2}})\b", "mezz_n", 1.0),
    (r"\bmezz(?:anine)?\b", 1.5, 0.7),
    (rf"\b(?:ground|grade|garden)\s*{_STOREY}{_END}", 0.0, 1.0),
    (rf"\b(?:{_STOREY}){_SEP}(?:g|gf|ground)\b", 0.0, 1.0),
    (rf"\b(?:main|street|entry|entrance|plaza|lobby|concourse)\s*{_STOREY}{_END}", 1.0, 0.9),
    (rf"\bupper\s*{_STOREY}{_END}", 2.0, 0.6),
    (rf"\b(?:roof|penthouse)\s*(?:plan|level|floor){_END}", None, 0.9),
    (rf"\b({_ORD})\s*{_STOREY}{_END}", "ord", 1.0),
    (rf"\b(\d{{1,3}})\s*(?:st|nd|rd|th)\s*{_STOREY}{_END}", "num", 1.0),
    (rf"\b(?:{_STOREY})\s+-(\d{{1,2}})\b(?!\s*(?:of|/))", "neg", 0.9),
    (rf"\b(?:{_STOREY}){_SEP}({_DIGITS})\b", "num", 1.0),
    (rf"\b(?:{_STOREY}){_SEP}({_CARD})\b", "card", 1.0),
    (rf"\b(\d{{1,2}})\s*(?:floor|level){_END}", "num", 0.8),
    (r"\b(\d{1,2})\s*/?\s*f\b", "num", 0.7),
]
_PHRASES = [(re.compile(p, re.I), how, s) for p, how, s in _PHRASES]
_NO = object()  # a phrase matched but its number is not a floor

# A mark standing alone, read only when it is printed big: "4", "04", "B", "B2", "G",
# "LL", "L3", "5R", "2M".
_MARK = re.compile(r"^(?:(\d{1,2})([a-z])?|(b|ll|lg|sb)(\d)?|(g|gf)|l(\d{1,2}))$", re.I)

_ROOM_NUMBER = re.compile(
    r"(?<![\w.,$/:#'\"-])([a-z]{1,2}[-.]?)?(\d{3,4})([a-z])?(?![\w%°'\"/:.,-])", re.I)
# Text that holds numbers that are not room numbers: dates, scales, phone numbers, areas.
_NOT_ROOMS = re.compile(
    r"date|rev\b|revision|scale|©|copyright|phone|tel\b|fax|printed|updated|\bsq\b|\bsf\b|sq\.?\s*ft|"
    r"\bm2\b|m²|\bft\b|\d{3}[-.)]\s*\d{3}[-.]\d{4}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.I)

_BUILDING_WORDS = re.compile(
    r"\b(?:hall|union|center|centre|building|bldg|library|tower|house|laborator(?:y|ies)|pavilion|annex|"
    r"gym(?:nasium)?|field\s*house|stadium|arena|museum|theat(?:er|re)|chapel|commons|complex|institute|"
    r"school|college|residence|apartments|auditorium|hospital|clinic|dormitory|science|engineering|"
    r"recreation|student|athletic|hotel|office\s*building|plaza|court)\b", re.I)
_FILLER = re.compile(
    r"\b(?:floor\s*plans?|plans?|layout|map|key|scale|north|drawing|sheet|overall|partial|area|"
    r"reflected|ceiling|furniture|life\s*safety|egress|existing|proposed|new|demo(?:lition)?|of|the)\b",
    re.I)


@dataclass
class _Line:
    text: str
    conf: float
    box: tuple  # x0, y0, x1, y1
    h: float

    @property
    def cy(self) -> float:
        return 0.5 * (self.box[1] + self.box[3])


def parse_floor(text: str):
    """Read a floor from wording: ``"Second Floor Plan"`` gives ``(2.0, "Second Floor")``.

    Returns ``(level, label)`` or None. ``level`` is None for a roof plan.
    """
    hit = _parse(text)
    return (hit[0], hit[1]) if hit else None


def _parse(text: str):
    """(level, label, strength, span) for the first floor wording in ``text``, or None."""
    if not text:
        return None
    t = text.replace("_", " ")
    for rx, how, strength in _PHRASES:
        m = rx.search(t)
        if not m:
            continue
        level = _level_of(how, m)
        if level is _NO:
            continue
        label = _tidy(m.group(0))
        return level, label, strength, m.span()
    return None


def _num(s: str):
    s = (s or "").lower().replace("o", "0").replace("l", "1").replace("i", "1")
    return int(s) if s.isdigit() else None


def _level_of(how, m):
    if how is None or isinstance(how, float):
        return how
    g = m.group(1) if m.groups() else None
    if how == "sub":
        return -2.0
    if how == "basement_n":
        n = _num(g) if g else 1
        return -float(n) if n else _NO
    if how == "basement_ord":
        return -float(_ORDINALS[g.lower()])
    if how == "mezz_n":
        n = _num(g)
        return n + 0.5 if n is not None and n < 100 else _NO
    if how == "mezz_ord":
        return _ORDINALS[g.lower()] + 0.5
    if how == "ord":
        return float(_ORDINALS[g.lower()])
    if how == "card":
        return float(_CARDINALS[g.lower()])
    if how == "neg":
        n = _num(g)
        return -float(n) if n else _NO
    if how == "num":
        n = _num(g)
        # "Level 0" is the ground floor; numbers above 120 are not floors (elevations,
        # room numbers after "level").
        return float(n) if n is not None and n <= 120 else _NO
    return _NO


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" -_#:.,")
    s = re.sub(r"(?i)(?<=[a-z])(?=(?:floor|level)\b)", " ", s)  # "SECONDFLOOR"
    if s.isupper() or s.islower():
        s = " ".join(_cap(w) for w in s.split(" "))
    return s


def _cap(w: str) -> str:
    if w[:1].isalpha():
        return w[:1].upper() + w[1:].lower()
    return w.lower() if re.fullmatch(r"(?i)\d+(?:st|nd|rd|th)", w) else w


def _mark_level(s: str):
    """Level of a lone mark ("4", "B", "5R"), or None."""
    m = _MARK.match(re.sub(r"[\s.:()\[\]]", "", s))
    if not m:
        return None
    num, suffix, base, base_n, ground, lvl = m.groups()
    if num is not None:
        n = int(num)
        if n == 0 or n > 60:
            return None
        return n + 0.5 if (suffix or "").lower() == "m" else float(n)
    if base is not None:
        n = int(base_n) if base_n else (2 if base.lower() == "sb" else 1)
        return -float(n)
    if ground is not None:
        return 0.0
    return float(int(lvl)) if lvl else None


# ---------------------------------------------------------------------------
# Reading the floor from all the text on a plan
# ---------------------------------------------------------------------------


def floor_from_texts(items) -> dict | None:
    """The floor shown by a plan, from its text.

    ``items`` are text lines with ``quad`` (4 corners), ``text`` and ``confidence``,
    such as :class:`floorplan_converter.text.TextItem`; any pixel scale works.
    Returns ``{"label", "level", "confidence", "text", "source"}`` (plus
    ``"building"`` when the title names one), or None when nothing says.
    """
    lines = []
    for it in items:
        s = re.sub(r"\s+", " ", str(it.text or "")).strip()
        if not s:
            continue
        q = np.asarray(it.quad, np.float64).reshape(-1, 2)
        x0, y0 = q.min(axis=0)
        x1, y1 = q.max(axis=0)
        h = float(getattr(it, "height", min(x1 - x0, y1 - y0)))
        lines.append(_Line(s, float(it.confidence), (x0, y0, x1, y1), max(h, 1e-6)))
    if not lines:
        return None

    words = [ln.h for ln in lines if len(re.sub(r"\W", "", ln.text)) >= 2 and ln.conf >= 0.5]
    typical = float(np.median(words)) if len(words) >= 3 else float(np.median([ln.h for ln in lines]))

    title = _best_title(lines, typical)
    vote = _room_vote(lines)
    building = _building(lines, typical, title)

    out = None
    if title is not None:
        level, label, tconf, raw, source = title
        conf = tconf
        if vote is not None:
            vlevel, vconf, _ = vote
            if vlevel == level:
                conf = 1 - (1 - tconf) * (1 - vconf)
                source += "+room_numbers"
            elif vconf > tconf:
                # The room numbers outvote a weak title (a misread mark, a stray word).
                title = None
            else:
                conf = tconf * (1 - 0.5 * vconf)
        if title is not None:
            out = {"label": label, "level": level, "confidence": conf, "text": raw, "source": source}
    if out is None and vote is not None:
        vlevel, vconf, numbers = vote
        out = {"label": None, "level": vlevel, "confidence": vconf,
               "text": " ".join(numbers[:12]), "source": "room_numbers"}
    if out is None:
        return None
    out["confidence"] = round(float(min(out["confidence"], 0.98)), 2)
    if building:
        out["building"] = building
    return out


def _neighbour_pairs(lines):
    """Pairs of lines that read on as one phrase: words on one row, or stacked lines."""
    if len(lines) < 2:
        return []
    b = np.array([ln.box for ln in lines], np.float64)
    h = np.array([ln.h for ln in lines], np.float64)
    x0, y0, x1, y1 = (b[:, k] for k in range(4))
    cy = 0.5 * (y0 + y1)
    hmax = np.maximum(h[:, None], h[None, :])
    similar = hmax / np.minimum(h[:, None], h[None, :]) <= 1.45
    same_row = np.abs(cy[:, None] - cy[None, :]) <= 0.45 * hmax
    hgap = np.maximum(x0[None, :] - x1[:, None], x0[:, None] - x1[None, :])
    vgap = np.maximum(y0[None, :] - y1[:, None], y0[:, None] - y1[None, :])
    overlap = np.minimum(x1[:, None], x1[None, :]) - np.maximum(x0[:, None], x0[None, :])
    narrow = np.minimum((x1 - x0)[:, None], (x1 - x0)[None, :])
    near = similar & np.where(same_row, hgap <= 1.3 * hmax, (vgap <= 0.9 * hmax) & (overlap >= 0.3 * narrow))
    ii, jj = np.nonzero(np.triu(near, 1))
    return [(lines[i], lines[j]) for i, j in zip(ii, jj)]


def _reading_order(a: _Line, b: _Line):
    h = max(a.h, b.h)
    if abs(a.cy - b.cy) <= 0.45 * h:
        return (a, b) if a.box[0] <= b.box[0] else (b, a)
    return (a, b) if a.cy < b.cy else (b, a)


def _best_title(lines, typical):
    """The strongest floor wording or lone mark: (level, label, confidence, text, source)."""
    cands = []  # (score, level, label, conf, raw, source)
    for ln in lines:
        hit = _parse(ln.text)
        if hit:
            cands.append(_phrase_cand(hit, ln.text, ln.h, ln.conf, typical))
    # Phrases broken over two lines or boxes: "Lower" / "Level", "LEVEL" "2".
    for a, b in _neighbour_pairs(lines):
        first, second = _reading_order(a, b)
        joined = f"{first.text} {second.text}"
        hit = _parse(joined)
        if hit and not _parse(first.text) and not _parse(second.text):
            cands.append(_phrase_cand(hit, joined, min(a.h, b.h), min(a.conf, b.conf), typical))

    # Big lone marks. Grid bubbles and floor pickers print many marks in a row, which
    # say nothing about this floor, so marks only count when there is one of them.
    marks = []
    for ln in lines:
        lvl = _mark_level(ln.text)
        if lvl is not None and ln.h >= 2.5 * typical:
            marks.append((ln, lvl))
    big_singles = [ln for ln in lines if ln.h >= 2.0 * typical and len(re.sub(r"\W", "", ln.text)) <= 2]
    if len(marks) == 1 and len(big_singles) <= 2 and not _among_labels(marks[0][0], lines, typical):
        ln, lvl = marks[0]
        size = min(1.0, (ln.h / typical - 1) / 1.5)
        # The recogniser gives single characters about 0.5 at best.
        conf = (0.45 + 0.2 * size) * (0.8 + 0.4 * min(ln.conf, 0.5))
        label = None if re.fullmatch(r"\d{1,2}", ln.text.strip()) else ln.text.strip()
        cands.append((conf, lvl, label, conf, ln.text, "mark"))
    if not cands:
        return None
    cands.sort(key=lambda c: -c[0])
    best = cands[0]
    rivals = [c for c in cands[1:] if c[1] != best[1] and c[0] >= 0.85 * best[0]]
    conf = best[3] * (0.6 if rivals else 1.0)
    return best[1], best[2], conf, best[4], best[5]


def _among_labels(mark: _Line, lines, typical: float) -> bool:
    """Whether a mark sits among the room labels (inside the plan), where a big
    "10" is more likely a table and chairs than a floor number."""
    words = [ln for ln in lines if ln is not mark and ln.conf >= 0.5 and ln.h <= 1.6 * typical
             and len(re.sub(r"\W", "", ln.text)) >= 2]
    if len(words) < 4:
        return False
    pts = np.array([[0.5 * (ln.box[0] + ln.box[2]), ln.cy] for ln in words], np.float32)
    hull = cv2.convexHull(pts)
    c = (float(0.5 * (mark.box[0] + mark.box[2])), float(mark.cy))
    return cv2.pointPolygonTest(hull, c, False) > 0


def _phrase_cand(hit, raw, h, ocr_conf, typical):
    level, label, strength, _ = hit
    size = float(np.clip((h / typical - 1) / 1.5, 0, 1))
    conf = strength * (0.6 + 0.3 * size) * (0.75 + 0.25 * min(1.0, ocr_conf))
    return (conf, level, label, conf, raw, "title")


def _room_vote(lines):
    """(level, confidence, numbers) from the room numbers, or None."""
    toks = []
    for ln in lines:
        if _NOT_ROOMS.search(ln.text):
            continue
        for m in _ROOM_NUMBER.finditer(ln.text):
            if int(m.group(2)) == 0:  # "000": stove burners, chairs
                continue
            pre = (m.group(1) or "").upper().strip("-.")
            toks.append((pre, m.group(2), m.group(0).strip()))
    if len(toks) < 3:
        return None
    # Letters in front of room numbers mean basement or ground floor only when no
    # other letters are used (else they name wings: A210, B210, C210). A letter seen
    # once is a stray (a sheet number like A-101).
    used = Counter(p for p, _, _ in toks if p)
    floor_letters = {p for p, n in used.items() if n >= 2} <= {"B", "LL", "LG", "SB", "G"}
    levels = []
    floor_codes = {"B": -1.0, "LL": -1.0, "LG": -1.0, "SB": -2.0, "G": 0.0}
    for pre, digits, raw in toks:
        if pre in floor_codes and floor_letters:
            lvl = floor_codes[pre]
        else:
            lvl = -1.0 if digits[0] == "0" else float(digits[0])
        levels.append((lvl, raw))
    counts = Counter(lvl for lvl, _ in levels)
    level, n = counts.most_common(1)[0]
    agree = n / len(levels)
    if n < 3 or agree < 0.6:
        return None
    conf = min(0.3 + 0.045 * n, 0.75) * min(1.0, (agree - 0.5) / 0.4)
    numbers = [raw for lvl, raw in levels if lvl == level]
    return level, conf, numbers


def _building(lines, typical, title):
    """A building name printed with the title, or as a big heading, or None."""
    names = []
    if title is not None:
        raw = title[3]
        hit = _parse(raw)
        if hit:
            s, e = hit[3]
            rest = raw[:s] + " " + raw[e:]
            rest = _clean_name(rest) if _BUILDING_WORDS.search(rest) else None
            if rest:
                names.append((2.0, rest))
    # Otherwise a heading: among the biggest words on the page (room names such as
    # "Great Hall" or "Leadership Center" are not), and not among the room labels.
    tallest = max((ln.h for ln in lines if ln.conf >= 0.5 and len(re.sub(r"\W", "", ln.text)) > 2), default=0.0)
    for ln in lines:
        if (ln.h < max(1.6 * typical, 0.8 * tallest) or ln.conf < 0.6 or not _BUILDING_WORDS.search(ln.text)
                or _parse(ln.text) or _among_labels(ln, lines, typical)):
            continue
        name = _clean_name(ln.text)
        if name:
            names.append((ln.h / typical, name))
    if not names:
        return None
    return max(names, key=lambda c: c[0])[1]


def _clean_name(s: str):
    s = _FILLER.sub(" ", s)
    s = re.sub(r"[|•·–—:\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;/")
    letters = re.sub(r"[^A-Za-z]", "", s)
    if len(letters) < 4 or len(s.split()) > 8:
        return None
    return s


# ---------------------------------------------------------------------------
# Public entry point: a quick text-only pass
# ---------------------------------------------------------------------------


def read_floor(image, *, ocr: str = "auto", pdf_page: int = 0, max_size: int = 2000) -> dict | None:
    """Read which floor a plan shows, without converting it.

    ``image`` takes the same inputs as :func:`convert` (bytes, path, array, PIL
    image, PDF). PDFs with a text layer are read directly; images and scanned PDFs
    go through OCR at up to ``max_size`` pixels. Returns the same dict as
    ``ConversionResult.meta["floor"]``, or None.
    """
    pdf = pdf_bytes(image)
    found = None
    if pdf is not None:
        found = floor_from_texts(pdf_text_items(pdf, pdf_page))
        if found is not None and found["confidence"] >= 0.5:
            return found
        # No text layer, or one that does not say (the plan is a picture in the PDF).
        img = load_image(pdf, pdf_page, 150)
    else:
        img = load_image(image)
    backend = text_mod.get_backend(ocr)
    if backend.name == "none":
        return found
    read = floor_from_texts(ocr_page(img, backend, max_size))
    if found is None or (read is not None and read["confidence"] > found["confidence"]):
        return read
    return found


def ocr_page(img: np.ndarray, backend, max_size: int = 2000) -> list:
    """OCR a whole page at a size that keeps titles and room numbers readable; text
    items come back in the image's own pixels."""
    h, w = img.shape[:2]
    longest = max(h, w)
    s = max_size / longest if longest > max_size else min(1000 / longest, 2.0) if longest < 1000 else 1.0
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC) if s != 1.0 else img
    if float(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).mean()) < 110:  # white on dark (blueprints)
        small = 255 - small
    try:
        items = backend.detect(small)
    except Exception:  # OCR must never take the caller down
        return []
    for it in items:
        it.quad = it.quad / s
    return items


def pdf_bytes(src):
    """The bytes of a PDF input (bytes or path), or None for anything else."""
    if isinstance(src, (str, os.PathLike)):
        try:
            with open(src, "rb") as fh:
                head = fh.read(5)
                if head != b"%PDF-":
                    return None
                return head + fh.read()
        except OSError:
            return None
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = bytes(src)
        return data if data[:5] == b"%PDF-" else None
    return None


def pdf_text_items(data: bytes, page: int = 0, size=None) -> list:
    """Text lines from a PDF page's text layer, in PDF points (y down), or in the
    pixels of the page rendered at ``size`` (width, height). Empty when the PDF has
    none (scans) or no PDF library is installed."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return []
    try:
        doc = pdfium.PdfDocument(data)
        if page >= len(doc):
            return []
        pg = doc[page]
        height = pg.get_height()
        sx = sy = 1.0
        if size is not None:
            sx, sy = size[0] / max(pg.get_width(), 1e-6), size[1] / max(height, 1e-6)
        tp = pg.get_textpage()
        items = []
        for k in range(tp.count_rects()):
            left, bottom, right, top = tp.get_rect(k)
            s = tp.get_text_bounded(left, bottom, right, top)
            s = re.sub(r"\s+", " ", s or "").strip()
            if not s:
                continue
            quad = np.array([[left, height - top], [right, height - top],
                             [right, height - bottom], [left, height - bottom]], np.float64) * (sx, sy)
            items.append(text_mod.TextItem(quad, s, 1.0))
        return items
    except Exception:
        return []
