"""Debug renderings of the pipeline's intermediate steps (PNG bytes)."""

from __future__ import annotations

import cv2
import numpy as np

KIND_COLORS = {
    "room": (222, 184, 135),
    "corridor": (200, 200, 200),
    "stair": (120, 170, 250),
    "elevator": (60, 120, 220),
    "restroom": (200, 140, 220),
    "outdoor": (150, 220, 150),
    "unknown": (180, 180, 180),
}


def _png(img: np.ndarray, max_side: int = 2000) -> bytes:
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1.0:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else b""


def render(bgr, sty, tr, W, gr, soft, labels, rooms, scale) -> dict:
    out = {}
    h, w = labels.shape

    # 1. Text: everything removed as text in red, OCR boxes in blue.
    t = cv2.cvtColor(np.where(sty.ink, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    t[tr.mask] = (0, 0, 255)
    for it in tr.items:
        cv2.polylines(t, [np.round(it.quad).astype(np.int32)], True, (255, 120, 0), 1)
    out["text"] = _png(t)

    # 2. Walls: heavy walls black, light walls dark gray, minor lines light gray, removed
    #    clutter pink,
    #    closed doorways red, colour-change boundaries blue.
    v = np.full((h, w, 3), 255, np.uint8)
    v[W.removed] = (200, 200, 255)
    v[W.mask] = (175, 175, 175)
    v[W.barrier] = (90, 90, 90)
    v[W.strong] = (0, 0, 0)
    v[soft] = (255, 150, 0)
    v[gr.mask] = (0, 0, 230)
    out["walls"] = _png(v)

    # 3. Rooms over the input.
    over = bgr.copy()
    color = np.zeros_like(over)
    rng = np.random.default_rng(7)
    for rid in np.unique(labels):
        if rid <= 0:
            continue
        color[labels == rid] = rng.integers(60, 230, 3)
    m = labels > 0
    over[m] = (0.45 * over[m] + 0.55 * color[m]).astype(np.uint8)
    for r in rooms:
        pts = np.round(np.array(r.polygon) * scale - 0.5).astype(np.int32)
        cv2.polylines(over, [pts], True, KIND_COLORS.get(r.kind, (0, 0, 0)), 2, cv2.LINE_AA)
        cx, cy = (np.array(r.centroid) * scale).astype(int)
        tag = r.number or r.name or r.id
        cv2.putText(over, str(tag)[:18], (int(cx) - 20, int(cy)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 2, cv2.LINE_AA)
        cv2.putText(over, str(tag)[:18], (int(cx) - 20, int(cy)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    out["rooms"] = _png(over)

    # 4. Final polygons only, coloured by kind.
    poly = np.full((h, w, 3), 255, np.uint8)
    for r in rooms:
        pts = np.round(np.array(r.polygon) * scale - 0.5).astype(np.int32)
        holes = [np.round(np.array(hh) * scale - 0.5).astype(np.int32) for hh in r.holes]
        cv2.fillPoly(poly, [pts] + holes, KIND_COLORS.get(r.kind, (180, 180, 180)))
        cv2.polylines(poly, [pts], True, (60, 60, 60), 1, cv2.LINE_AA)
    out["polygons"] = _png(poly)
    return out
