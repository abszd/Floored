"""Port of the original locked-in-backend converter.

This is kept as a dependency-light fallback while the rebuilt converter in
``converter/`` matures. The algorithm is the same as before (crop the plan,
watershed on the distance transform of free space), with three changes:
EasyOCR/Tesseract text removal is replaced by a connected-component filter so
it runs on a plain server, watershed seeds come from door-closed free space so
rooms joined by a doorway are not merged, and the output follows
``ConversionResult``.
"""

from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt
from skimage.segmentation import watershed

from .base import ConversionResult, Room


def _decode(image: bytes) -> np.ndarray:
    arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise ValueError("could not decode image")
    return arr


def _ink_mask(gray: np.ndarray) -> np.ndarray:
    """Dark strokes (walls, text, symbols) as 255."""
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return ink


def _remove_small_marks(ink: np.ndarray) -> np.ndarray:
    """Drop isolated small components: text, dimension ticks, furniture glyphs."""
    h, w = ink.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    limit = 0.04 * min(h, w)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        keep[i] = max(bw, bh) >= limit
    return np.where(keep[labels], 255, 0).astype(np.uint8)


def _main_region(walls: np.ndarray) -> np.ndarray:
    """Filled mask of the building: everything enclosed by the outer walls."""
    h, w = walls.shape
    k = max(3, int(0.01 * min(h, w)) | 1)
    closed = cv2.morphologyEx(walls, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("no plan found in image")
    main = max(contours, key=cv2.contourArea)
    if cv2.contourArea(main) < 0.01 * h * w:
        raise ValueError("plan region too small")
    mask = np.zeros_like(walls)
    cv2.drawContours(mask, [main], 0, 255, -1)
    return mask


def _ring(contour: np.ndarray, eps: float) -> list[list[float]]:
    approx = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
    return [[float(x), float(y)] for x, y in approx]


class LegacyConverter:
    name = "legacy"

    def __init__(self, door_ratios: tuple[float, ...] = (0.02, 0.035, 0.05, 0.07), min_room_ratio: float = 0.001) -> None:
        self.door_ratios = door_ratios
        self.min_room_ratio = min_room_ratio

    def convert(self, image: bytes) -> ConversionResult:
        bgr = _decode(image)
        h, w = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        walls = _remove_small_marks(_ink_mask(gray))
        region = _main_region(walls)

        outline_contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        outline_contour = max(outline_contours, key=cv2.contourArea)
        eps = 0.002 * cv2.arcLength(outline_contour, True)
        outline = _ring(outline_contour, eps)

        free = cv2.bitwise_and(region, cv2.bitwise_not(walls))
        dist = distance_transform_edt(free > 0)
        # Seeds: free space after thickening the walls enough to close doorways,
        # so rooms joined only by a door get separate markers. The original
        # global-threshold seeding merged such rooms into one. Door widths
        # vary between drawings, so try a few and keep the one that separates
        # the most room-sized regions.
        min_seed = 0.25 * self.min_room_ratio * h * w
        best = None
        for ratio in self.door_ratios:
            r = max(2, int(ratio * min(h, w)))
            thick = cv2.dilate(walls, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
            seeds = cv2.bitwise_and(free, cv2.bitwise_not(thick))
            n, lab, stats, _ = cv2.connectedComponentsWithStats(seeds, 8)
            big = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_seed]
            if best is None or len(big) > best[0]:
                markers = np.zeros_like(lab)
                for k, i in enumerate(big, start=1):
                    markers[lab == i] = k
                best = (len(big), markers)
        markers = best[1]
        labels = watershed(-dist, markers, mask=free > 0)

        min_area = self.min_room_ratio * h * w
        rooms: list[Room] = []
        overlay = bgr.copy()
        rng = np.random.default_rng(0)
        for label in np.unique(labels):
            if label == 0:
                continue
            mask = (labels == label).astype(np.uint8)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in contours:
                area = float(cv2.contourArea(c))
                if area < min_area:
                    continue
                ring = _ring(c, 1.5)
                if len(ring) < 3:
                    continue
                rooms.append(Room(id=f"r{len(rooms) + 1}", polygon=ring, area_px=area))
                color = tuple(int(v) for v in rng.integers(80, 255, 3))
                cv2.drawContours(overlay, [c], -1, color, -1)

        debug = {
            "walls": cv2.imencode(".png", walls)[1].tobytes(),
            "rooms": cv2.imencode(".png", cv2.addWeighted(bgr, 0.4, overlay, 0.6, 0))[1].tobytes(),
        }
        return ConversionResult(
            image_size=(w, h),
            rooms=rooms,
            outline=outline,
            debug_images=debug,
            meta={"converter": self.name},
        )
