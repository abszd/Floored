"""Decode inputs (bytes, paths, arrays, PDFs) into a BGR image and a working copy."""

from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np


class InputError(ValueError):
    """The input could not be decoded as an image."""


def load_image(src, pdf_page: int = 0, pdf_dpi: int = 200) -> np.ndarray:
    """Return a BGR uint8 image from bytes, a file path, a PIL image or a numpy array."""
    if isinstance(src, np.ndarray):
        return _normalize_array(src)
    if isinstance(src, (str, os.PathLike)):
        with open(src, "rb") as fh:
            src = fh.read()
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = bytes(src)
        if data[:5] == b"%PDF-":
            return _render_pdf(data, pdf_page, pdf_dpi)
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
        if img is None:
            img = _decode_with_pillow(data)
        if img is None:
            raise InputError("could not decode image data")
        return _normalize_array(img)
    if hasattr(src, "convert") and hasattr(src, "size"):  # PIL.Image
        return _normalize_array(np.asarray(src.convert("RGBA"))[..., [2, 1, 0, 3]])
    raise InputError(f"unsupported input type: {type(src).__name__}")


def _decode_with_pillow(data: bytes):
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(BytesIO(data))
        im.seek(0)
        return np.asarray(im.convert("RGBA"))[..., [2, 1, 0, 3]]
    except Exception:
        return None


def _normalize_array(img: np.ndarray) -> np.ndarray:
    if img.dtype != np.uint8:
        img = img.astype(np.float64)
        hi = img.max() if img.size else 1.0
        img = np.clip(img * (255.0 / (hi if hi > 1.0 else 1.0)), 0, 255).astype(np.uint8)
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.ndim == 3 and img.shape[2] == 1:
        return cv2.cvtColor(img[..., 0], cv2.COLOR_GRAY2BGR)
    if img.ndim == 3 and img.shape[2] == 4:
        # Composite transparency onto white: transparent PNG plans are common and
        # their colour channels under alpha=0 are often black.
        alpha = img[..., 3:4].astype(np.float32) / 255.0
        rgb = img[..., :3].astype(np.float32)
        out = rgb * alpha + 255.0 * (1.0 - alpha)
        return np.clip(out, 0, 255).astype(np.uint8)
    if img.ndim == 3 and img.shape[2] == 3:
        return np.ascontiguousarray(img)
    raise InputError(f"unsupported image shape {img.shape}")


def _render_pdf(data: bytes, page: int, dpi: int) -> np.ndarray:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise InputError("PDF input needs PyMuPDF: pip install 'floorplan-converter[pdf]'") from exc
    doc = fitz.open(stream=data, filetype="pdf")
    if page >= doc.page_count:
        raise InputError(f"PDF has {doc.page_count} page(s), page {page} requested")
    pix = doc.load_page(page).get_pixmap(dpi=dpi, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if pix.n == 3 else _normalize_array(img)


@dataclass
class WorkingImage:
    """The image the pipeline runs on, and how it maps back to the original."""

    bgr: np.ndarray
    scale: float  # working pixels per original pixel
    original_size: tuple  # (width, height)

    def to_original(self, pts: np.ndarray) -> np.ndarray:
        """Map working-image pixel coordinates (x, y) to original-image coordinates."""
        pts = np.asarray(pts, dtype=np.float64)
        return (pts + 0.5) / self.scale - 0.5


def make_working(img: np.ndarray, max_size: int, min_size: int) -> WorkingImage:
    h, w = img.shape[:2]
    longest = max(h, w)
    scale = 1.0
    if longest > max_size:
        scale = max_size / longest
    elif longest < min_size:
        scale = min(min_size / longest, 3.0)
    if scale != 1.0:
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        work = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=interp)
        scale = work.shape[1] / w
    else:
        work = img.copy()
    return WorkingImage(work, scale, (w, h))
