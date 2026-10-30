"""Finding text so it can be (a) removed from the linework and (b) used as room labels.

Two complementary detectors are combined:

* An OCR engine (RapidOCR's PP-OCR models by default, Tesseract as a fallback).
  It finds words reliably and reads them, which is what gives rooms their names.
* A shape-based detector that groups small, thin connected components into
  lines of characters. It needs no models, catches what OCR misses (tiny
  numbers, single letters next to symbols, dimension strings), and also sweeps
  away rows of small furniture symbols and thin dashed lines, which are never
  walls either.

Text pixels are removed from the ink mask, except where a long straight line
(a wall) runs through the text box, so labels that overlap walls do not cut them.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass

import cv2
import numpy as np
from scipy import ndimage as ndi

log = logging.getLogger(__name__)


@dataclass
class TextItem:
    quad: np.ndarray  # (4, 2) float, working-image pixels
    text: str
    confidence: float

    @property
    def center(self) -> np.ndarray:
        return self.quad.mean(axis=0)

    @property
    def height(self) -> float:
        q = self.quad
        a = np.linalg.norm(q[0] - q[3])
        b = np.linalg.norm(q[1] - q[2])
        c = np.linalg.norm(q[0] - q[1])
        # The box's shorter side is the text height (handles vertical text).
        return float(min((a + b) / 2, c)) if c > 0 else float((a + b) / 2)


# ---------------------------------------------------------------------------
# OCR backends
# ---------------------------------------------------------------------------


class OcrBackend:
    name = "none"

    def detect(self, bgr: np.ndarray) -> list:  # pragma: no cover - interface
        return []


class _RapidOcr(OcrBackend):
    name = "rapidocr"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self.engine = RapidOCR()

    def detect(self, bgr):
        eng = self.engine
        h, w = bgr.shape[:2]
        if h < 32 or w < 32:
            return []
        # rapidocr-onnxruntime 1.4 calls these text_det and text_rec; 1.2 used longer names.
        det = getattr(eng, "text_detector", None) or eng.text_det
        rec = getattr(eng, "text_recognizer", None) or eng.text_rec
        det.postprocess_op.box_thresh = 0.45
        det.postprocess_op.unclip_ratio = 1.6
        boxes = det(bgr)[0]
        if boxes is None or len(boxes) == 0:
            return []
        boxes = eng.sorted_boxes(boxes)
        crops = eng.get_crop_img_list(bgr, boxes)
        recs = rec(crops)[0]
        items = []
        for box, r in zip(boxes, recs):
            items.append(TextItem(np.asarray(box, np.float64).reshape(4, 2), str(r[0]).strip(), float(r[1])))
        return items


class _Tesseract(OcrBackend):
    name = "tesseract"

    def __init__(self):
        import pytesseract

        pytesseract.get_tesseract_version()  # raises when the binary is missing
        self.pt = pytesseract

    def detect(self, bgr):
        pt = self.pt
        data = pt.image_to_data(bgr, output_type=pt.Output.DICT, config="--oem 3 --psm 11")
        items = []
        for i, txt in enumerate(data["text"]):
            txt = (txt or "").strip()
            conf = float(data["conf"][i])
            if not txt or conf < 30:
                continue
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            quad = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float64)
            items.append(TextItem(quad, txt, conf / 100.0))
        return items


@functools.lru_cache(maxsize=None)
def get_backend(name: str = "auto") -> OcrBackend:
    """Return an OCR backend. Engines load once per process."""
    name = (name or "none").lower()
    order = {"auto": ["rapidocr", "tesseract"], "rapidocr": ["rapidocr"], "tesseract": ["tesseract"], "none": []}
    if name not in order:
        raise ValueError(f"unknown OCR backend {name!r}")
    for cand in order[name]:
        try:
            return _RapidOcr() if cand == "rapidocr" else _Tesseract()
        except Exception as exc:  # missing package or binary
            log.debug("OCR backend %s unavailable: %s", cand, exc)
            if name != "auto":
                raise
    return OcrBackend()


# ---------------------------------------------------------------------------
# Text detection and removal
# ---------------------------------------------------------------------------


@dataclass
class TextResult:
    items: list  # TextItem from OCR (with strings)
    mask: np.ndarray  # bool: ink pixels that belong to text or text-like clutter
    text_height: float  # typical character height in working pixels (0 if unknown)
    stroke: float  # typical text stroke width in working pixels (0 if unknown)


def find_text(bgr: np.ndarray, ink: np.ndarray, backend: OcrBackend) -> TextResult:
    h, w = ink.shape
    items = []
    if backend.name != "none":
        try:
            items = backend.detect(bgr)
        except Exception as exc:  # OCR must never take the conversion down
            log.warning("OCR failed (%s); continuing without it", exc)
            items = []
    # Drop absurd boxes (giant false positives over hatching or whole walls).
    items = [it for it in items if 3 <= it.height <= 0.08 * max(h, w)]

    ink_u8 = ink.astype(np.uint8)
    dt = cv2.distanceTransform(ink_u8, cv2.DIST_L2, 3)

    heights = np.array([it.height for it in items if it.confidence >= 0.5]) if items else np.array([])
    text_h = float(np.median(heights)) if heights.size >= 2 else 0.0

    ocr_mask = np.zeros((h, w), np.uint8)
    for it in items:
        q = _expand_quad(it.quad, 0.12 * it.height)
        cv2.fillPoly(ocr_mask, [np.round(q).astype(np.int32)], 1)
    ocr_mask = ocr_mask.astype(bool)

    # Stroke width of text, measured on ink inside confident OCR boxes.
    stroke = 0.0
    sel = ocr_mask & ink
    if sel.sum() > 50:
        ridge = sel & (dt >= ndi.maximum_filter(dt, size=3))
        vals = dt[ridge]
        if vals.size:
            stroke = float(2 * np.percentile(vals, 60))

    shape_mask, shape_h = _shape_text(ink, dt, text_h, stroke)
    if text_h == 0 and shape_h > 0:
        text_h = shape_h

    # Inside OCR boxes remove ink except long straight lines (walls through labels)
    # and strokes clearly thicker than letters.
    protect = np.zeros((h, w), bool)
    if ocr_mask.any():
        L = int(max(15, 2.4 * (text_h or 12)))
        hk = cv2.getStructuringElement(cv2.MORPH_RECT, (L, 1))
        vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, L))
        protect = (cv2.morphologyEx(ink_u8, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(ink_u8, cv2.MORPH_OPEN, vk)).astype(bool)
        if stroke > 0:
            thick = dt > max(stroke * 1.1, 1.5)
            thick = cv2.dilate(thick.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=int(np.ceil(stroke))).astype(bool) & ink
            protect |= thick
    text_mask = (ocr_mask & ink & ~protect) | shape_mask
    return TextResult(items, text_mask, text_h, stroke)


def _expand_quad(q: np.ndarray, pad: float) -> np.ndarray:
    c = q.mean(axis=0)
    out = []
    for p in q:
        v = p - c
        n = np.linalg.norm(v)
        out.append(p + (v / n * pad * 1.41 if n > 0 else 0))
    return np.array(out)


def _shape_text(ink: np.ndarray, dt: np.ndarray, text_h: float, stroke: float):
    """Group small thin components into text lines. Returns (mask, typical height)."""
    h, w = ink.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    if n <= 1:
        return np.zeros_like(ink), 0.0
    idx = np.arange(1, n)
    maxdt = ndi.maximum(dt, labels, idx)
    x, y, bw, bh, area = (stats[1:, i] for i in range(5))
    longest = np.maximum(bw, bh)
    char_max = text_h * 1.7 if text_h > 0 else 0.03 * min(h, w)
    char_max = max(char_max, 8)
    sw = 2 * maxdt
    cand = (
        (longest >= 4)
        & (longest <= char_max)
        & (sw <= np.maximum(0.34 * longest, 2.0))
        & (area >= 6)
    )
    if stroke > 0:
        cand &= sw <= max(2.2 * stroke, stroke + 2)
    ci = np.nonzero(cand)[0]
    if ci.size < 2:
        return np.zeros_like(ink), 0.0

    # Union-find over neighbouring candidates of similar height on a shared baseline.
    parent = np.arange(ci.size)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    cx0, cy0 = x[ci].astype(np.float64), y[ci].astype(np.float64)
    cx1, cy1 = cx0 + bw[ci], cy0 + bh[ci]
    ch, cw = bh[ci].astype(np.float64), bw[ci].astype(np.float64)
    from scipy.spatial import cKDTree

    centers = np.stack([(cx0 + cx1) / 2, (cy0 + cy1) / 2], axis=1)
    tree = cKDTree(centers)
    radius = char_max * 1.6
    pairs = tree.query_pairs(radius, output_type="ndarray")
    for a, b in pairs:
        ha, hb = ch[a], ch[b]
        # horizontal text
        vo = min(cy1[a], cy1[b]) - max(cy0[a], cy0[b])
        hgap = max(cx0[b] - cx1[a], cx0[a] - cx1[b])
        ok_h = vo >= 0.45 * min(ha, hb) and max(ha, hb) <= 2.4 * min(ha, hb) and hgap <= 0.9 * max(ha, hb)
        # vertical text
        wa, wb = cw[a], cw[b]
        ho = min(cx1[a], cx1[b]) - max(cx0[a], cx0[b])
        vgap = max(cy0[b] - cy1[a], cy0[a] - cy1[b])
        ok_v = ho >= 0.45 * min(wa, wb) and max(wa, wb) <= 2.4 * min(wa, wb) and vgap <= 0.9 * max(wa, wb)
        if ok_h or ok_v:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    roots = np.array([find(i) for i in range(ci.size)])
    counts = np.bincount(roots, minlength=ci.size)
    keep = np.zeros(n, bool)
    good = counts[roots] >= 3
    # Two-character groups only when they look like a word (side by side, wide).
    two = counts[roots] == 2
    if two.any():
        for r in np.unique(roots[two]):
            mem = np.nonzero(roots == r)[0]
            gx0, gx1 = cx0[mem].min(), cx1[mem].max()
            gy0, gy1 = cy0[mem].min(), cy1[mem].max()
            if (gx1 - gx0) >= 1.2 * (gy1 - gy0) or (gy1 - gy0) >= 1.2 * (gx1 - gx0):
                good[mem] = True
    keep[ci[good] + 1] = True
    mask = keep[labels]
    if mask.any():
        mask = cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ink
    typical = float(np.median(ch[good])) if good.any() else 0.0
    return mask, typical


# ---------------------------------------------------------------------------
# Label parsing
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\(?\b([A-Z]{0,2}-?\d{2,5}[A-Z]?)\b\)?")


def split_label(texts: list) -> tuple:
    """Turn the OCR lines found in a room into (name, number)."""
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return None, None
    number = None
    name_parts = []
    for t in texts:
        m = _NUMBER_RE.fullmatch(t.strip().strip("()").strip()) if t else None
        if m and number is None and any(ch.isdigit() for ch in t):
            number = m.group(1)
            continue
        inner = re.search(r"\(([A-Z]{0,2}-?\d{2,5}[A-Z]?)\)", t)
        if inner and number is None:
            number = inner.group(1)
            t = (t[: inner.start()] + t[inner.end():]).strip()
        if t:
            name_parts.append(t)
    name = " ".join(name_parts).strip() or None
    return name, number
