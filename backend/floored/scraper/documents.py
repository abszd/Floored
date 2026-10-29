"""Turn downloaded files into page images (PDF pages are rendered)."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

MAX_SIDE = 3000
PDF_MAX_PAGES = 40


@dataclass
class PageImage:
    index: int
    png: bytes
    bgr: np.ndarray
    text: str = ""


def _limit(bgr: np.ndarray) -> np.ndarray:
    h, w = bgr.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        bgr = cv2.resize(bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return bgr


def _encode(bgr: np.ndarray) -> bytes:
    return cv2.imencode(".png", bgr)[1].tobytes()


def from_image(data: bytes) -> list[PageImage]:
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        return []
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif arr.shape[2] == 4:
        # Transparent backgrounds should read as white paper, not black.
        alpha = arr[..., 3:4].astype(np.float32) / 255
        arr = (arr[..., :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
    arr = _limit(arr)
    return [PageImage(0, _encode(arr), arr)]


def from_pdf(data: bytes, dpi: int = 150) -> list[PageImage]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    out = []
    try:
        for i in range(min(len(pdf), PDF_MAX_PAGES)):
            page = pdf[i]
            try:
                text = page.get_textpage().get_text_range()
            except Exception:
                text = ""
            pil = page.render(scale=dpi / 72).to_pil().convert("RGB")
            bgr = _limit(cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR))
            out.append(PageImage(i, _encode(bgr), bgr, " ".join(text.split())[:2000]))
    finally:
        pdf.close()
    return out


def load(data: bytes, content_type: str = "", url: str = "") -> list[PageImage]:
    if data[:5] == b"%PDF-" or "pdf" in content_type or url.lower().endswith(".pdf"):
        return from_pdf(data)
    return from_image(data)
