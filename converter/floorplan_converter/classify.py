"""Room labels and types.

Labels come from OCR text whose centre falls inside the room. Types come first
from the label ("STAIR B", "Women", "Corridor", "Elev."), then from what the
drawing shows: tread hatching for stairs, a crossed box for elevators, a long
narrow shape touching many rooms for corridors, and an unpainted area in a
painted plan for decks, terraces and courtyards.
"""

from __future__ import annotations

import re

import cv2
import numpy as np
from scipy import ndimage as ndi

from .text import split_label

_KIND_PATTERNS = [
    ("stair", re.compile(r"\b(stair|stairs|stairway|stairwell|staircase|escalier)\b", re.I)),
    ("elevator", re.compile(r"\b(elev\.?|elevator|elevators|lift|lifts)\b", re.I)),
    ("restroom", re.compile(r"\b(rest\s?rooms?|toilets?|w\.?c\.?|lav(atory|atories)?|bath(room)?s?|showers?|men|women|mens|womens|men's|women's|unisex|all[- ]gender)\b", re.I)),
    ("corridor", re.compile(r"\b(corridors?|corr\.?|hall|hallway|passage|circulation|vestibule)\b", re.I)),
    ("outdoor", re.compile(r"\b(deck|terrace|patio|balcony|courtyard|porch|roof garden)\b", re.I)),
]


def kind_from_label(label: str):
    if not label:
        return None
    for kind, pat in _KIND_PATTERNS:
        if pat.search(label):
            return kind
    return None


def assign_texts(labels: np.ndarray, items: list, max_dist: float) -> dict:
    """Map room id -> list of TextItems whose centre lies in (or right next to) the room."""
    H, W = labels.shape
    out = {}
    if not items:
        return out
    dist, (iy, ix) = None, (None, None)
    for k, it in enumerate(items):
        cx, cy = it.center
        x, y = int(round(cx)), int(round(cy))
        if not (0 <= x < W and 0 <= y < H):
            continue
        rid = int(labels[y, x])
        if rid == 0:
            # Labels written across a wall or doorway: take the nearest room within reach.
            if dist is None:
                dist, (iy, ix) = ndi.distance_transform_edt(labels == 0, return_indices=True)
            if dist[y, x] <= max_dist:
                rid = int(labels[iy[y, x], ix[y, x]])
        if rid > 0:
            out.setdefault(rid, []).append((k, it))
    return out


def reading_order(entries: list) -> list:
    """Sort text items top-to-bottom, then left-to-right within a line."""
    if not entries:
        return []
    hs = np.median([it.height for _, it in entries]) or 1.0
    return sorted(entries, key=lambda e: (round(e[1].center[1] / (0.7 * hs)), e[1].center[0]))


def label_room(entries: list):
    items = [it for _, it in reading_order(entries) if it.confidence >= 0.5 and it.text]
    if not items:
        return None, None, None
    full = " ".join(it.text for it in items).strip()
    name, number = split_label([it.text for it in items])
    return full or None, name, number


def has_cross(ink: np.ndarray, room: np.ndarray) -> bool:
    """A crossed box (elevator symbol): ink along both diagonals of the room."""
    ys, xs = np.nonzero(room)
    if xs.size < 30:
        return False
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    w, h = x1 - x0, y1 - y0
    if w < 8 or h < 8 or max(w, h) > 1.8 * min(w, h):
        return False
    n = int(max(w, h))
    t = np.linspace(0.12, 0.88, n)
    hits = []
    for (ax, ay), (bx, by) in (((x0, y0), (x1, y1)), ((x0, y1), (x1, y0))):
        px = np.round(ax + (bx - ax) * t).astype(int)
        py = np.round(ay + (by - ay) * t).astype(int)
        sub = ink[np.clip(py, 0, ink.shape[0] - 1), np.clip(px, 0, ink.shape[1] - 1)]
        # Allow the line to wobble by a pixel.
        near = sub | ink[np.clip(py + 1, 0, ink.shape[0] - 1), np.clip(px, 0, ink.shape[1] - 1)] \
            | ink[np.clip(py, 0, ink.shape[0] - 1), np.clip(px + 1, 0, ink.shape[1] - 1)]
        hits.append(near.mean())
    return min(hits) >= 0.6


def shape_stats(room: np.ndarray) -> dict:
    """Elongation of a region: skeleton length over mean width."""
    from skimage.morphology import skeletonize

    area = float(room.sum())
    sk = skeletonize(room)
    L = float(sk.sum())
    width = area / max(L, 1.0)
    return {"area": area, "length": L, "width": width, "elongation": L / max(width, 1.0)}


def neighbour_count(labels: np.ndarray, rid: int, sl, reach: int) -> int:
    H, W = labels.shape
    y0, y1 = max(0, sl[0].start - reach), min(H, sl[0].stop + reach)
    x0, x1 = max(0, sl[1].start - reach), min(W, sl[1].stop + reach)
    sub = labels[y0:y1, x0:x1]
    m = (sub == rid).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * reach + 1, 2 * reach + 1))
    ring = cv2.dilate(m, k).astype(bool) & (sub != rid)
    v = np.unique(sub[ring])
    return int((v > 0).sum())
