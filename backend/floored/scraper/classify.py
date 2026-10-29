"""Is this image a floor plan? A fast heuristic, good enough to rank candidates.

Floor plans are mostly white, nearly colourless, and full of long straight
horizontal and vertical lines. Photos and logos are not.
"""

from __future__ import annotations

import cv2
import numpy as np


def plan_likeness(image: np.ndarray) -> float:
    """Score in [0, 1]; above ~0.5 usually means a plan."""
    if image is None or image.size == 0:
        return 0.0
    h, w = image.shape[:2]
    if min(h, w) < 200:
        return 0.0
    scale = 1000 / max(h, w)
    if scale < 1:
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    light = float((gray > 200).mean())
    colourless = float((hsv[..., 1] < 50).mean())
    ink = float((gray < 110).mean())

    edges = cv2.Canny(gray, 50, 150)
    min_len = int(0.05 * max(gray.shape))
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=min_len, maxLineGap=4)
    axis_len = 0.0
    total_len = 0.0
    if lines is not None:
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            length = float(np.hypot(x2 - x1, y2 - y1))
            total_len += length
            angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))) % 90
            if angle < 3 or angle > 87:
                axis_len += length
    line_density = min(1.0, total_len / (4 * sum(gray.shape)))
    axis_share = axis_len / total_len if total_len else 0.0

    score = (
        0.25 * min(1.0, light / 0.6)
        + 0.15 * colourless
        + 0.3 * line_density
        + 0.2 * axis_share
        + 0.1 * (1.0 if 0.01 < ink < 0.35 else 0.0)
    )
    return round(float(np.clip(score, 0, 1)), 3)
