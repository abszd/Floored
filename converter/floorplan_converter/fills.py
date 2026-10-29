"""Room fills: colour-change boundaries and page-coloured (outdoor) areas.

Wayfinding maps (like the Memorial Union plans) paint rooms gray or in colours
and corridors in a lighter tone, and often leave doorways as plain gaps. Where
two differently painted areas meet without a wall, the colour change is itself
the boundary. Those soft boundaries are returned as a mask that the room
segmentation treats like a (weak) wall.
"""

from __future__ import annotations

import cv2
import numpy as np


def lab_image(bgr: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] *= 100.0 / 255.0
    lab[..., 1:] -= 128.0
    return lab


def fill_boundaries(bgr: np.ndarray, ignore: np.ndarray, min_len: float, delta_e: float = 6.0) -> np.ndarray:
    """Pixels where the fill colour changes, away from ink and text."""
    smooth = cv2.medianBlur(bgr, 5)
    lab = lab_image(smooth)
    d = 2
    dx = np.zeros(lab.shape[:2], np.float32)
    dy = np.zeros(lab.shape[:2], np.float32)
    dx[:, d:-d] = np.sqrt(((lab[:, 2 * d:] - lab[:, :-2 * d]) ** 2).sum(axis=2))
    dy[d:-d, :] = np.sqrt(((lab[2 * d:, :] - lab[:-2 * d, :]) ** 2).sum(axis=2))
    edge = np.maximum(dx, dy) >= delta_e
    edge &= ~ignore
    if not edge.any():
        return edge
    n, lab_cc, stats, _ = cv2.connectedComponentsWithStats(edge.astype(np.uint8), connectivity=8)
    span = np.maximum(stats[:, 2], stats[:, 3])
    keep = span >= min_len
    keep[0] = False
    edge = keep[lab_cc]
    # Thin to a clean 1-2 px line.
    from skimage.morphology import skeletonize

    sk = skeletonize(edge)
    return cv2.dilate(sk.astype(np.uint8), np.ones((2, 2), np.uint8)).astype(bool)


class PaperModel:
    """The page colour as a smooth function of position, fitted to the paper around the
    plan. Scans and photos are lit unevenly, so one background colour would make an
    unpainted room in the darker half of the page look painted."""

    def __init__(self, lab: np.ndarray, outside: np.ndarray, max_samples: int = 40000):
        H, W = outside.shape
        ys, xs = np.nonzero(outside)
        self.shape = (H, W)
        if xs.size < 50:
            self.coef = None
            self.const = np.median(lab.reshape(-1, 3), axis=0)
            return
        if xs.size > max_samples:
            pick = np.random.default_rng(0).choice(xs.size, max_samples, replace=False)
            xs, ys = xs[pick], ys[pick]
        A = self._design(xs, ys)
        v = lab[ys, xs].astype(np.float64)
        keep = np.ones(len(xs), bool)
        coef = None
        for _ in range(3):
            coef, *_ = np.linalg.lstsq(A[keep], v[keep], rcond=None)
            r = np.sqrt(((A @ coef - v) ** 2).sum(axis=1))
            mad = float(np.median(r[keep])) + 1e-6
            keep = r <= max(3.0, 3.0 * mad)
        self.coef = coef
        self.const = None

    def _design(self, xs, ys):
        H, W = self.shape
        x = xs / max(W, 1) - 0.5
        y = ys / max(H, 1) - 0.5
        return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=1)

    def at(self, xs, ys) -> np.ndarray:
        if self.coef is None:
            return np.broadcast_to(self.const, (len(xs), 3))
        return self._design(np.asarray(xs, np.float64), np.asarray(ys, np.float64)) @ self.coef


def page_like(lab: np.ndarray, model: PaperModel, regions: np.ndarray, ids, ink: np.ndarray) -> dict:
    """For each region id, whether its paint matches the page (unpainted)."""
    out = {}
    valid = ~ink
    for rid in ids:
        ys, xs = np.nonzero((regions == rid) & valid)
        if xs.size < 10:
            out[rid] = False
            continue
        if xs.size > 5000:
            pick = np.random.default_rng(rid).choice(xs.size, 5000, replace=False)
            xs, ys = xs[pick], ys[pick]
        med = np.median(lab[ys, xs] - model.at(xs, ys), axis=0)
        out[rid] = bool(abs(med[0]) < 3.0 and np.hypot(med[1], med[2]) < 4.0)
    return out


def painted_fraction(lab: np.ndarray, model: PaperModel, inside: np.ndarray) -> float:
    """Share of ``inside`` pixels whose colour differs from the page."""
    ys, xs = np.nonzero(inside)
    if xs.size < 50:
        return 0.0
    if xs.size > 40000:
        pick = np.random.default_rng(1).choice(xs.size, 40000, replace=False)
        xs, ys = xs[pick], ys[pick]
    diff = np.sqrt(((lab[ys, xs] - model.at(xs, ys)) ** 2).sum(axis=1))
    return float((diff > 5.0).mean())


def painted_regions(lab: np.ndarray, model: PaperModel, fp: np.ndarray, min_area: float, width: float) -> np.ndarray:
    """Painted areas (a colour unlike the page, lighter than ink) joined to the building
    footprint ``fp`` and at least ``min_area`` px, outside ``fp``."""
    H, W = fp.shape
    f = 4 if max(H, W) > 1600 else 2
    h, w = max(1, H // f), max(1, W // f)
    small = cv2.resize(lab, (w, h), interpolation=cv2.INTER_AREA)
    ys, xs = np.mgrid[0:h, 0:w]
    ref = model.at((xs.ravel() + 0.5) * W / w - 0.5, (ys.ravel() + 0.5) * H / h - 0.5).reshape(h, w, 3)
    diff = np.sqrt(((small - ref) ** 2).sum(axis=2))
    paint = ((diff > 8.0) & (small[..., 0] > 30.0)).astype(np.uint8)
    # Drop text strokes, thin lines and edges: paint fills areas.
    paint = cv2.morphologyEx(paint, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    paint = cv2.resize(paint, (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)
    n, comp, stats, _ = cv2.connectedComponentsWithStats(paint.astype(np.uint8), connectivity=8)
    if n <= 1:
        return np.zeros_like(fp)
    r = int(np.ceil(2 * width)) + 1
    touch = cv2.dilate(fp.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))).astype(bool)
    hit = np.bincount(comp[touch], minlength=n) > 0
    keep = hit & (stats[:, cv2.CC_STAT_AREA] >= min_area)
    keep[0] = False
    return keep[comp] & ~fp
