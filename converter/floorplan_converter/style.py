"""Drawing-style analysis: background, inversion, local contrast and the ink mask.

Everything downstream works on "ink": pixels that are clearly darker than the
paper around them. Measuring darkness against the *local* paper (rather than one
global threshold) is what lets the same code handle white pages, gray or colour
room fills, tinted or unevenly lit scans, and inverted blueprints.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from skimage.filters import apply_hysteresis_threshold


@dataclass
class Style:
    inverted: bool  # light lines on a dark page (blueprints, dark mode)
    background_bgr: tuple
    lightness: np.ndarray  # float32 in [0, 1]; paper is light after inversion
    chroma: np.ndarray  # float32, 0 for grays, ~20 for pastels, 60+ for saturated colours
    paper: np.ndarray  # float32 local paper lightness
    contrast: np.ndarray  # float32 in [0, 1]: how much darker than the local paper
    ink: np.ndarray  # bool
    ink_threshold: float
    colored_ink: bool  # the linework itself is coloured (e.g. blue CAD lines)
    has_fills: bool = False  # rooms are filled with gray or colour (set later)


def analyze(bgr: np.ndarray) -> Style:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    L = lab[..., 0] / 255.0
    chroma = np.hypot(lab[..., 1] - 128.0, lab[..., 2] - 128.0)

    bg_bgr, bg_l = _background(bgr, L)
    inverted = bg_l < 0.42
    if inverted:
        L = 1.0 - L

    h, w = L.shape
    diag = float(np.hypot(h, w))
    paper = _local_paper(L, max(15, int(diag * 0.035)))
    contrast = np.clip(1.0 - L / np.maximum(paper, 0.05), 0.0, 1.0)

    cand = contrast[contrast > 0.08]
    if cand.size > 100:
        t = _otsu(cand)
        t = float(np.clip(t, 0.22, 0.4))
    else:
        t = 0.3
    # Hysteresis: faint anti-aliased pixels count as ink when they touch clear ink, so
    # one-pixel lines stay connected (rooms must not leak through them).
    strong = (contrast > t) | (L < 0.22)
    weak = contrast > 0.55 * t
    ink = apply_hysteresis_threshold(np.where(strong, 2.0, np.where(weak, 1.0, 0.0)), 0.5, 1.5)

    # Coloured marks (icons, highlight fills, coloured text) are not walls, unless the
    # whole drawing is done in a colour.
    ink_chroma = chroma[ink]
    colored_ink = bool(ink_chroma.size and np.median(ink_chroma) > 18.0)
    if not colored_ink and ink_chroma.size:
        limit = max(22.0, float(np.percentile(ink_chroma, 50)) * 3.0)
        ink &= chroma < limit
    return Style(inverted, bg_bgr, L, chroma, paper, contrast, ink, t, colored_ink)


def _background(bgr, L):
    h, w = L.shape
    b = max(2, int(min(h, w) * 0.03))
    border = np.concatenate([bgr[:b].reshape(-1, 3), bgr[-b:].reshape(-1, 3), bgr[:, :b].reshape(-1, 3), bgr[:, -b:].reshape(-1, 3)])
    q = (border // 16).astype(np.int32)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    mode = np.bincount(keys).argmax()
    sel = border[keys == mode]
    bg = tuple(int(v) for v in np.median(sel, axis=0))
    bg_l = cv2.cvtColor(np.uint8([[bg]]), cv2.COLOR_BGR2LAB)[0, 0, 0] / 255.0
    # A plan drawn edge to edge can have little border; fall back to the global mode.
    whole = (L * 15).astype(np.int32)
    global_mode = np.bincount(whole.ravel(), minlength=16).argmax() / 15.0
    if abs(global_mode - bg_l) > 0.5:
        bg_l = global_mode
    return bg, float(bg_l)


def _local_paper(L: np.ndarray, k: int) -> np.ndarray:
    """Lightness of the paper (or fill) under each pixel: a grey closing that
    erases dark strokes thinner than k, computed at reduced resolution."""
    f = 4 if min(L.shape) > 400 else 1
    small = cv2.resize(L, (max(1, L.shape[1] // f), max(1, L.shape[0] // f)), interpolation=cv2.INTER_AREA) if f > 1 else L
    ks = max(3, (k // f) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (ks, ks))
    # max-filter with a slightly smaller kernel first, so area-averaged thin lines vanish
    closed = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
    closed = cv2.blur(closed, (3, 3))
    if f > 1:
        closed = cv2.resize(closed, (L.shape[1], L.shape[0]), interpolation=cv2.INTER_LINEAR)
    # Never below the pixel itself (keeps big uniform areas at contrast 0).
    return np.maximum(closed, L)


def _otsu(values: np.ndarray) -> float:
    hist, edges = np.histogram(values, bins=128, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    p = hist / hist.sum()
    omega = np.cumsum(p)
    centers = (edges[:-1] + edges[1:]) / 2
    mu = np.cumsum(p * centers)
    mu_t = mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = (mu_t * omega - mu) ** 2 / (omega * (1 - omega))
    sigma_b = np.nan_to_num(sigma_b)
    return float(centers[int(np.argmax(sigma_b))])


def detect_fills(style: Style, footprint: np.ndarray) -> bool:
    """True when room interiors are painted (gray or colour) rather than left as paper."""
    inside = footprint & ~style.ink
    n = int(inside.sum())
    if n < 100:
        return False
    L = style.lightness[inside]
    C = style.chroma[inside]
    page = float(np.percentile(style.lightness[~footprint], 90)) if (~footprint).any() else 1.0
    painted = (L < page - 0.05) | (C > 8.0)
    return bool(painted.mean() > 0.3)
