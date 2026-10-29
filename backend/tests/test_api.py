"""End to end: crawl a fake site, match against a fake Nominatim, convert, place."""

import math

import httpx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon, mapping, shape

from floored import http, jobs
from floored.placement.transform import Placement
from synth import draw_plan

LON, LAT = -89.4003, 43.0761
BEARING = 18.0


def _footprint():
    # The synthetic plan's walls span pixels (60,60)-(840,540) of a 900x600 image.
    truth = Placement(LON, LAT, 90, 60, rotation_deg=BEARING)
    ring = [truth.pixel_to_lonlat(x, y, 900, 600) for x, y in ((60, 60), (840, 60), (840, 540), (60, 540))]
    return mapping(Polygon(ring))


def _handler():
    png, _ = draw_plan()
    fp = _footprint()
    site = {
        "/": '<title>Campus floor plans</title><h2>Memorial Union</h2>'
        '<p>800 Langdon St, Madison, WI 53706</p><a href="/plans/mu_floor2.png">Second Floor</a>',
    }

    def handle(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "nominatim.openstreetmap.org":
            return httpx.Response(
                200,
                json=[
                    {
                        "lat": str(LAT),
                        "lon": str(LON),
                        "name": "Memorial Union",
                        "display_name": "Memorial Union, 800, Langdon Street, Madison, Wisconsin, 53706",
                        "osm_type": "way",
                        "osm_id": 123,
                        "category": "building",
                        "type": "university",
                        "importance": 0.5,
                        "boundingbox": ["43.0757", "43.0765", "-89.4009", "-89.3997"],
                        "geojson": fp,
                    }
                ],
            )
        if host == "uni.example.edu":
            if path == "/robots.txt":
                return httpx.Response(404)
            if path in site:
                return httpx.Response(200, html=site[path])
            if path == "/plans/mu_floor2.png":
                return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(404)

    return handle


@pytest.fixture()
def client(settings):
    http.set_transport(httpx.MockTransport(_handler()))
    jobs.synchronous = True
    from floored.api.app import app

    with TestClient(app) as c:
        yield c
    jobs.synchronous = False


def test_crawl_to_placed_floor(client):
    src = client.post(
        "/api/sources",
        json={"name": "Campus", "seed_urls": ["https://uni.example.edu/"], "location_hint": "Madison, WI"},
    ).json()
    job = client.post(f"/api/sources/{src['id']}/crawl").json()
    status = client.get(f"/api/jobs/{job['job_id']}").json()
    assert status["status"] == "done", status
    assert status["result"]["found"] == 1

    cands = client.get("/api/candidates").json()
    assert len(cands) == 1
    c = cands[0]
    assert c["building_name"] == "Memorial Union"
    assert c["level_guess"] == 2
    assert c["match_confidence"] >= 0.85

    buildings = client.get("/api/buildings").json()
    assert len(buildings) == 1
    b = client.get(f"/api/buildings/{buildings[0]['id']}").json()
    assert len(b["floors"]) == 1
    floor = client.get(f"/api/floors/{b['floors'][0]['id']}").json()
    # A rectangle fits equally well turned 180 degrees, so it is placed
    # (north-up wins the tie) but flagged for a human to confirm.
    assert floor["status"] == "needs_review", floor
    assert floor["fit_score"] > 0.9
    diff = (floor["placement"]["rotation_deg"] - BEARING + 180) % 360 - 180
    assert abs(diff) < 2

    rooms = [f for f in floor["geojson"]["features"] if f["properties"]["feature"] == "room"]
    assert len(rooms) >= 6
    fp = shape(_footprint())
    inside = sum(fp.buffer(1e-5).contains(shape(r["geometry"])) for r in rooms)
    assert inside == len(rooms)

    # The map layer lists the building with its footprint.
    fc = client.get("/api/buildings.geojson").json()
    assert fc["features"][0]["geometry"]["type"] == "Polygon"
    assert fc["features"][0]["properties"]["review_count"] == 1


def test_upload_rename_room_and_move(client):
    b = client.post(
        "/api/buildings",
        json={"name": "Union", "place": {"lat": LAT, "lon": LON, "osm_id": "way/123", "footprint": _footprint()}},
    ).json()
    png, _ = draw_plan()
    r = client.post(f"/api/buildings/{b['id']}/floors", files={"file": ("p.png", png, "image/png")}, data={"level": "3"})
    assert r.status_code == 201, r.text
    floor_id = r.json()["floor"]["id"]
    floor = client.get(f"/api/floors/{floor_id}").json()
    assert floor["name"] == "3rd floor"
    room = next(f for f in floor["geojson"]["features"] if f["properties"]["feature"] == "room")

    floor = client.patch(f"/api/floors/{floor_id}/rooms/{room['properties']['room_id']}", json={"label": "Great Hall", "kind": "room"}).json()
    assert any(f["properties"].get("label") == "Great Hall" for f in floor["geojson"]["features"])
    hits = client.get("/api/search", params={"q": "great"}).json()
    assert hits["rooms"][0]["floor_id"] == floor_id

    p = dict(floor["placement"])
    p["center_lon"] += 0.0001
    moved = client.put(f"/api/floors/{floor_id}/placement", json=p).json()
    assert moved["fit_score"] < floor["fit_score"]
    assert len(moved["corners"]) == 4

    refit = client.post(f"/api/floors/{floor_id}/autofit").json()
    assert refit["fit"]["iou"] > 0.9

    assert client.get(f"/api/buildings/{b['id']}/levels/3.geojson").json()["features"]
