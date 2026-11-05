"""Is this image a floor plan? A fast heuristic, good enough to rank candidates.

A floor plan is a line drawing: flat areas of paper or fill colour separated
by sharp, long, straight lines that run in two perpendicular directions.
Photos are the opposite: smooth shading (skin, sky, walls in perspective)
almost everywhere and few long straight lines. These measurements carry
the decision:

- ``structure``: how much of the image's edge ink lies on long straight runs
  in one perpendicular pair of directions (tolerating a few degrees of scan
  tilt), and how much of that line work there is.
- ``flat``: the share of pixels in perfectly flat surroundings, which rules
  out textures such as brick that are full of straight lines too.
- ``paper``: drawings sit on one dominant background colour (paper, or a
  blueprint's blue); a photo of a facade full of windows does not.
- ``photo``: the share of the image covered by continuous-tone shading, which
  drawings, fills and text (all sharp-edged) don't have. Even a photo that
  covers a fifth of a white flyer is enough to rule it out.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

_WORK_SIDE = 900
_TILTS = (0.0, -1.5, 1.5, -3.0, 3.0, -4.5, 4.5, -6.0, 6.0)


@dataclass
class PlanFeatures:
    structure: float  # 0..1, long perpendicular line work
    photo: float  # share of the image that is continuous-tone shading
    paper: float  # share of the most common colour (paper or background)
    flat: float  # share of pixels in perfectly flat surroundings (paper, fills)
    axis_share: float
    line_length: float  # long-run length in image spans (height + width)
    tilt: float

    @property
    def score(self) -> float:
        photo_ok = float(np.clip(1 - (self.photo - 0.025) / 0.065, 0, 1))
        flat_ok = float(np.clip((self.flat - 0.6) / 0.15, 0, 1))
        paper_ok = float(np.clip((self.paper - 0.15) / 0.25, 0, 1))
        return round(self.structure * photo_ok * flat_ok * paper_ok, 3)


def _work_image(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    s = _WORK_SIDE / max(h, w)
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    return cv2.resize(image, (max(1, round(w * s)), max(1, round(h * s))), interpolation=interp)


def _long_runs(edges: np.ndarray, tilt: float, length: int) -> int:
    if tilt:
        h, w = edges.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), tilt, 1.0)
        edges = cv2.warpAffine(edges, m, (w, h), flags=cv2.INTER_NEAREST)
    horiz = cv2.morphologyEx(edges, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    vert = cv2.morphologyEx(edges, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    return int(np.count_nonzero(horiz | vert))


def features(image: np.ndarray) -> PlanFeatures:
    img = cv2.medianBlur(_work_image(image), 3)  # drops JPEG and scan speckle
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    q = (img // 32).astype(np.int32)
    codes = (q[..., 0] * 64 + q[..., 1] * 8 + q[..., 2]).ravel()
    paper = float(np.bincount(codes, minlength=512).max() / codes.size)

    # Continuous tone: gentle gradients that are not just the soft rim of a
    # sharp line or letter.
    g = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
    mag = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) / 4
    near_sharp = cv2.dilate((mag >= 12).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    shading = ((mag > 2) & (mag < 12) & ~near_sharp).astype(np.float32)
    b = 30
    hb, wb = h // b, w // b
    if hb and wb:
        blocks = shading[: hb * b, : wb * b].reshape(hb, b, wb, b).mean(axis=(1, 3))
        photo = float((blocks > 0.3).mean())
    else:
        photo = 0.0

    # Drawings are mostly perfectly flat paper and fill; textures (brick,
    # foliage, fabric) are not, even when they are full of straight lines.
    gf = gray.astype(np.float32)
    mean = cv2.blur(gf, (5, 5))
    flat = float((cv2.blur(gf * gf, (5, 5)) - mean * mean < 9).mean())

    # Line work: edges thickened by a pixel so slightly tilted lines stay unbroken.
    edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((3, 3), np.uint8))
    edge_px = max(int(np.count_nonzero(edges)), 1)
    run_len = max(15, int(0.04 * max(h, w)))
    best_px, tilt = max((_long_runs(edges, t, run_len), t) for t in _TILTS)
    axis_share = best_px / edge_px
    line_length = best_px / 3 / (h + w)
    structure = float(np.clip((axis_share - 0.2) / 0.35, 0, 1) * np.clip(line_length / 5, 0, 1))
    return PlanFeatures(
        structure=round(structure, 3),
        photo=round(photo, 3),
        paper=round(paper, 3),
        flat=round(flat, 3),
        axis_share=round(axis_share, 3),
        line_length=round(line_length, 2),
        tilt=tilt,
    )


def plan_likeness(image: np.ndarray) -> float:
    """Score in [0, 1]; above ~0.45 usually means a plan."""
    if image is None or image.size == 0 or min(image.shape[:2]) < 200:
        return 0.0
    return features(image).score
