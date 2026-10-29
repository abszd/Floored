import math

import pytest
from shapely.geometry import Polygon, mapping, shape

from floored.converters.base import ConversionResult, Room
from floored.geo.projection import LocalFrame
from floored.placement.fit import _iou, fit_to_footprint, plan_outline
from floored.placement.geojson import floor_geojson
from floored.placement.transform import Placement

LON, LAT = -89.4, 43.07


def _footprint(width_m, height_m, bearing_deg, lon=LON, lat=LAT):
    frame = LocalFrame(lon, lat)
    t = math.radians(bearing_deg)
    pts = []
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        u, v = u * width_m / 2, v * height_m / 2
        pts.append(frame.to_lonlat(u * math.cos(t) + v * math.sin(t), -u * math.sin(t) + v * math.cos(t)))
    return mapping(Polygon(pts))


def test_round_trip():
    p = Placement(LON, LAT, 120, 80, rotation_deg=33, mirror=True)
    lon, lat = p.pixel_to_lonlat(123, 45, 1000, 700)
    x, y = p.lonlat_to_pixel(lon, lat, 1000, 700)
    assert x == pytest.approx(123, abs=1e-6) and y == pytest.approx(45, abs=1e-6)


def test_bearing_convention():
    # Bearing 90: the top of the image points east.
    p = Placement(LON, LAT, 100, 100, rotation_deg=90)
    e, n = p.pixel_to_local(50, 0, 100, 100)
    assert e == pytest.approx(50) and n == pytest.approx(0, abs=1e-9)


def test_from_bounds_matches_old_resizer():
    p = Placement.from_bounds(-89.401, 43.07, -89.399, 43.071)
    lon, lat = p.pixel_to_lonlat(0, 0, 10, 10)
    assert lon == pytest.approx(-89.401) and lat == pytest.approx(43.071)


@pytest.mark.parametrize("bearing", [0, 25, 90, 137, 260])
def test_fit_recovers_rotation_and_scale(bearing):
    # An L-shaped plan so orientation is unambiguous.
    w, h = 1000, 800
    outline = [[100, 100], [900, 100], [900, 400], [500, 400], [500, 700], [100, 700]]
    conv = ConversionResult(image_size=(w, h), rooms=[], outline=outline)
    truth = Placement(LON, LAT, 80, 64, rotation_deg=bearing)  # 0.08 m/px
    fp = mapping(Polygon([truth.pixel_to_lonlat(x, y, w, h) for x, y in outline]))
    fit = fit_to_footprint(conv, fp)
    assert fit is not None
    assert fit.iou > 0.97
    diff = (fit.placement.rotation_deg - bearing + 180) % 360 - 180
    assert abs(diff) < 1.5
    assert fit.placement.width_m == pytest.approx(80, rel=0.03)
    assert not fit.ambiguous


def test_fit_rectangle_flags_ambiguity():
    w, h = 1000, 800
    outline = [[100, 100], [900, 100], [900, 700], [100, 700]]
    conv = ConversionResult(image_size=(w, h), rooms=[], outline=outline)
    fit = fit_to_footprint(conv, _footprint(60, 45, 20))
    assert fit.iou > 0.95
    # 180 degrees off fits a rectangle just as well.
    assert fit.ambiguous


def test_outline_from_rooms_when_converter_has_none():
    rooms = [
        Room("a", [[100, 100], [495, 100], [495, 400], [100, 400]]),
        Room("b", [[505, 100], [900, 100], [900, 400], [505, 400]]),
    ]
    conv = ConversionResult(image_size=(1000, 500), rooms=rooms)
    poly = plan_outline(conv)
    assert poly.area == pytest.approx(800 * 300, rel=0.03)


def test_geojson_areas_in_square_metres():
    conv = ConversionResult(
        image_size=(100, 100), rooms=[Room("a", [[0, 0], [50, 0], [50, 50], [0, 50]], area_px=2500, label="101")]
    )
    gj = floor_geojson(conv, Placement(LON, LAT, 20, 20), floor_id="f1", level=2)
    room = gj["features"][0]
    assert room["properties"]["area_m2"] == pytest.approx(100)
    ring = room["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]
    frame = LocalFrame(LON, LAT)
    local = Polygon([frame.to_local(*p) for p in ring])
    assert local.area == pytest.approx(100, rel=1e-3)
