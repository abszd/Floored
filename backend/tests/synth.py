"""Synthetic floor plans for tests: rooms drawn with walls, labels and furniture."""

from __future__ import annotations

import cv2
import numpy as np


def draw_plan(w: int = 900, h: int = 600, *, labels: bool = True, doors: bool = True) -> tuple[bytes, dict]:
    """A 3x2 grid of rooms plus a corridor. Returns PNG bytes and ground truth."""
    img = np.full((h, w, 3), 255, np.uint8)
    m = 60  # margin around the building
    t = 8  # wall thickness
    x0, y0, x1, y1 = m, m, w - m, h - m
    cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), t)
    corridor_y = (y0 + y1) // 2
    cv2.line(img, (x0, corridor_y - 40), (x1, corridor_y - 40), (0, 0, 0), t)
    cv2.line(img, (x0, corridor_y + 40), (x1, corridor_y + 40), (0, 0, 0), t)
    cols = [x0, x0 + (x1 - x0) // 3, x0 + 2 * (x1 - x0) // 3, x1]
    for cx in cols[1:-1]:
        cv2.line(img, (cx, y0), (cx, corridor_y - 40), (0, 0, 0), t)
        cv2.line(img, (cx, corridor_y + 40), (cx, y1), (0, 0, 0), t)
    if doors:
        for i in range(3):
            mid = (cols[i] + cols[i + 1]) // 2
            cv2.line(img, (mid - 20, corridor_y - 40), (mid + 20, corridor_y - 40), (255, 255, 255), t + 2)
            cv2.line(img, (mid - 20, corridor_y + 40), (mid + 20, corridor_y + 40), (255, 255, 255), t + 2)
    if labels:
        for i in range(3):
            mid = (cols[i] + cols[i + 1]) // 2
            cv2.putText(img, f"10{i + 1}", (mid - 25, y0 + 70), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
            cv2.putText(img, f"20{i + 1}", (mid - 25, y1 - 50), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
        # A table and chairs, which should not become rooms.
        cv2.rectangle(img, (x0 + 40, y1 - 120), (x0 + 80, y1 - 90), (0, 0, 0), 2)
    ok, buf = cv2.imencode(".png", img)
    truth = {"outline_px": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], "rooms": 7, "size": (w, h)}
    return buf.tobytes(), truth
