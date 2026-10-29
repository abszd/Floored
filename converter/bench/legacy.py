"""The college-project converter (locked-in-backend ``processors.py``), ported for comparison.

The steps and constants are unchanged: inpaint OCR boxes, crop the plan by a
flood fill plus a large closing, then a distance-transform watershed seeded from
local maxima. EasyOCR is swapped for the OCR backend the new converter uses, so the
comparison is about segmentation, not about which OCR engine found the text.
"""

from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt, maximum_filter
from skimage.morphology import remove_small_objects
from skimage.segmentation import watershed

from floorplan_converter import text


def _remove_text(image, backend):
    mask = np.zeros(image.shape[:2], np.uint8)
    for it in backend.detect(image) if backend is not None else []:
        pts = np.asarray(it.quad, np.int32)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        cv2.rectangle(mask, (int(x0), int(y0)), (int(x1), int(y1)), 255, -1)
    out = cv2.inpaint(image, mask, 5, cv2.INPAINT_NS)
    return cv2.inpaint(out, mask, 3, cv2.INPAINT_TELEA)


def _extract_floor_plan(image, backend):
    image = _remove_text(image, backend)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY_INV)
    height, width = gray.shape
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    max_gap = 0
    for contour in contours:
        for x, y in contour[:, 0]:
            max_gap = max(max_gap, min(x, y, width - x, height - y))
    mp = float(np.clip((max_gap / min(height, width)) * 1.2, 0.01, 0.04))
    mask = np.zeros((height + 2, width + 2), np.uint8)
    flood = closed.copy()
    cv2.floodFill(flood, mask, (0, 0), 255)
    floor_plan = closed | cv2.bitwise_not(flood)
    kernel = np.ones((int(height * mp), int(width * mp)), np.uint8)
    floor_plan = cv2.morphologyEx(floor_plan, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(floor_plan, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid = [c for c in contours if cv2.contourArea(c) > height * width * 0.01]
    if not valid:
        raise RuntimeError("no plan found")
    main = max(valid, key=cv2.contourArea)
    m = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(m, [main], 0, 255, -1)
    result = cv2.bitwise_and(image, image, mask=m)
    x, y, w, h = cv2.boundingRect(main)
    return result[y:y + h, x:x + w], (x, y)


def _remove_gaps(image, peak_multiplier=0.15):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dist = distance_transform_edt(binary)
    peaks = ((dist == maximum_filter(dist, size=60)) & (dist == maximum_filter(dist, size=20))
             | (dist > peak_multiplier * dist.max()))
    markers = cv2.connectedComponents(np.uint8(peaks))[1]
    labels = watershed(-dist, markers, mask=binary)
    min_size = image.shape[0] * image.shape[1] * 0.0001
    border = np.unique(np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]))
    cleared = labels.copy()
    for lab in border:
        if np.sum(labels == lab) < min_size:
            cleared[labels == lab] = 0
    return remove_small_objects(cleared, min_size=min_size)


def legacy_labels(bgr: np.ndarray, ocr: str = "auto") -> np.ndarray:
    """Room label image (0 = no room) in the input's pixel grid."""
    backend = text.get_backend(ocr)
    backend = None if backend.name == "none" else backend
    crop, (x, y) = _extract_floor_plan(bgr, backend)
    lab = _remove_gaps(crop)
    out = np.zeros(bgr.shape[:2], np.int32)
    out[y:y + lab.shape[0], x:x + lab.shape[1]] = lab
    return out
