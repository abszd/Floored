from shapely.geometry import Polygon

from floored.converters import get_converter
from floored.converters.base import ConversionResult
from synth import draw_plan


def test_legacy_converter_finds_rooms_and_outline(settings):
    png, truth = draw_plan()
    conv = get_converter("legacy").convert(png)
    assert conv.image_size == truth["size"]
    # 6 rooms + corridor; watershed may split the long corridor.
    assert 6 <= len(conv.rooms) <= 10
    outline = Polygon(conv.outline)
    assert outline.intersection(Polygon(truth["outline_px"])).area / outline.union(Polygon(truth["outline_px"])).area > 0.95
    assert set(conv.debug_images) >= {"rooms", "walls"}


def test_round_trip_dict():
    png, _ = draw_plan()
    conv = get_converter("legacy").convert(png)
    again = ConversionResult.from_dict(conv.to_dict())
    assert [r.polygon for r in again.rooms] == [r.polygon for r in conv.rooms]
