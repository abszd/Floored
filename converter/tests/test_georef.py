import numpy as np

from floorplan_converter import ConversionResult, Room, to_geojson
from floorplan_converter.georef import fit_affine


def _apply(T, x, y):
    return T @ np.array([x, y, 1.0])


def test_two_points_give_a_similarity_with_y_down():
    lon0, lat0, d = -89.4, 43.07, 1e-3
    T = fit_affine([((0, 0), (lon0, lat0)), ((100, 0), (lon0 + d, lat0))])
    assert np.allclose(_apply(T, 0, 0), [lon0, lat0])
    assert np.allclose(_apply(T, 100, 0), [lon0 + d, lat0])
    # Image y points down, so a point below maps south, not north (no mirroring).
    assert np.allclose(_apply(T, 0, 100), [lon0, lat0 - d])


def test_three_points_give_an_exact_affine():
    pts = [((0, 0), (10.0, 50.0)), ((200, 0), (10.2, 50.0)), ((0, 100), (10.0, 49.9))]
    T = fit_affine(pts)
    for (x, y), q in pts:
        assert np.allclose(_apply(T, x, y), q)


def test_bounds_place_the_outline_in_the_box():
    outline = [[10.0, 20.0], [110.0, 20.0], [110.0, 70.0], [10.0, 70.0]]
    room = Room(id="r1", polygon=[[12.0, 22.0], [60.0, 22.0], [60.0, 68.0], [12.0, 68.0]], label="Office 101")
    res = ConversionResult(image_size=[200, 100], outline=outline, rooms=[room],
                           footprint=[{"polygon": outline, "holes": []}])
    gj = to_geojson(res, bounds={"south": 43.0, "north": 43.001, "west": -89.002, "east": -89.0})
    ring = np.array(gj["features"][0]["geometry"]["coordinates"][0])
    assert np.isclose(ring[:, 0].min(), -89.002) and np.isclose(ring[:, 0].max(), -89.0)
    assert np.isclose(ring[:, 1].min(), 43.0) and np.isclose(ring[:, 1].max(), 43.001)
    rooms = [f for f in gj["features"] if f["properties"]["feature"] == "room"]
    assert rooms[0]["properties"]["label"] == "Office 101"
