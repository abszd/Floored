"""Tunable settings for the converter.

The defaults are chosen to work across drawing styles without tuning. Most sizes
are derived from the drawing itself (wall thickness, door widths, plan size), so
the knobs here are relative factors rather than pixel counts.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class ConverterOptions:
    # --- input -------------------------------------------------------------
    #: Longest side, in pixels, that the image is processed at. Larger inputs are
    #: downsampled (results are mapped back to original pixels).
    max_working_size: int = 2600
    #: Smaller inputs are upsampled to at least this longest side so thin walls survive.
    min_working_size: int = 1400
    #: Page to render when the input is a PDF (0-based). Needs PyMuPDF.
    pdf_page: int = 0
    pdf_dpi: int = 200
    #: When the plan covers only part of the sheet, run again on a crop around it so
    #: its thin lines get more working resolution (coordinates stay in full-image pixels).
    auto_crop: bool = True

    # --- text --------------------------------------------------------------
    #: "auto" (RapidOCR, then Tesseract, then none), "rapidocr", "tesseract" or "none".
    #: Without OCR, text is still removed using shape analysis, but rooms get no labels.
    ocr: str = "auto"

    # --- rooms -------------------------------------------------------------
    #: "interior" gives each room's floor area inside its walls (rooms separated by
    #: wall gaps). "centerline" grows rooms to the wall centre lines so they tile the
    #: building with no gaps.
    room_extent: str = "interior"
    #: Rooms smaller than this fraction of the building's floor area are merged into
    #: a neighbour (or dropped when they have none). Also bounded by wall thickness.
    min_room_fraction: float = 0.0012
    #: Close door openings up to this many typical door widths (double doors ~ 2).
    max_gap_doors: float = 2.2
    #: Split rooms joined by an unclosed opening narrower than this fraction of the
    #: smaller side's width. Keeps big or L-shaped rooms whole.
    neck_ratio: float = 0.62
    #: Use fill colour changes as room boundaries in colour-filled plans.
    use_fill_boundaries: bool = True

    # --- geometry ----------------------------------------------------------
    #: Snap nearly horizontal and vertical edges to the plan's dominant axes.
    orthogonalize: bool = True
    #: Polygon simplification tolerance as a fraction of wall thickness.
    simplify_factor: float = 0.45

    # --- output ------------------------------------------------------------
    #: Include intermediate images (PNG bytes) in the result.
    debug: bool = False

    def with_(self, **changes) -> "ConverterOptions":
        return replace(self, **changes)

    @classmethod
    def from_kwargs(cls, **kwargs) -> "ConverterOptions":
        names = {f.name for f in fields(cls)}
        unknown = set(kwargs) - names
        if unknown:
            raise TypeError(f"unknown converter option(s): {', '.join(sorted(unknown))}")
        return cls(**kwargs)
