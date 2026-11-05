"""Floorplan image to room polygons.

    from floorplan_converter import convert
    result = convert(open("plan.png", "rb").read())
    result.outline        # building footprint, original-image pixels
    result.rooms[0].polygon, result.rooms[0].label, result.rooms[0].kind
    to_geojson(result, bounds={...})   # rooms on a map
    read_floor(open("plan.png", "rb").read())   # {"label": "Second Floor", "level": 2.0, ...}
    detect_sheets(open("sheet.pdf", "rb").read())   # several floors on one sheet: where each is
"""

__version__ = "0.3.0"

from .api import convert, detect_sheets  # noqa: E402
from .floor import parse_floor, read_floor  # noqa: E402
from .georef import to_geojson  # noqa: E402
from .options import ConverterOptions  # noqa: E402
from .result import ROOM_KINDS, ConversionResult, Door, Room, TextLabel, Wall  # noqa: E402

__all__ = [
    "convert",
    "detect_sheets",
    "read_floor",
    "parse_floor",
    "to_geojson",
    "ConverterOptions",
    "ConversionResult",
    "Room",
    "Door",
    "Wall",
    "TextLabel",
    "ROOM_KINDS",
    "__version__",
]
