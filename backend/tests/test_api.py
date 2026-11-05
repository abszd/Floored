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


def test_relocate_moves_floors_without_reconverting(client, monkeypatch):
    b = client.post(
        "/api/buildings",
        json={"name": "Union", "place": {"lat": LAT, "lon": LON, "osm_id": "way/123", "footprint": _footprint()}},
    ).json()
    png, _ = draw_plan()
    floor_id = client.post(f"/api/buildings/{b['id']}/floors", files={"file": ("p.png", png, "image/png")}).json()["floor"]["id"]
    before = client.get(f"/api/floors/{floor_id}").json()

    from floored import pipeline

    monkeypatch.setattr(pipeline, "get_converter", lambda *a, **k: pytest.fail("the converter ran again"))
    shifted = shape(_footprint())
    from shapely.affinity import translate

    shifted = mapping(translate(shifted, xoff=0.5, yoff=-0.3))  # tens of km away
    c = shape(shifted).centroid
    r = client.post(
        f"/api/buildings/{b['id']}/relocate",
        json={"address": "1 New Street", "place": {"lat": c.y, "lon": c.x, "osm_id": "way/999", "footprint": shifted}},
    )
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["address"] == "1 New Street" and moved["osm_id"] == "way/999"
    after = client.get(f"/api/floors/{floor_id}").json()
    assert after["placement"]["center_lon"] == pytest.approx(before["placement"]["center_lon"] + 0.5, abs=1e-4)
    assert after["fit_score"] > 0.9
    rooms = lambda f: sorted(x["properties"]["room_id"] for x in f["geojson"]["features"] if x["properties"]["feature"] == "room")
    assert rooms(after) == rooms(before)

    # Another building can't take the same outline.
    other = client.post("/api/buildings", json={"name": "Other", "place": {"lat": LAT, "lon": LON + 1}}).json()
    clash = client.post(f"/api/buildings/{other['id']}/relocate", json={"place": {"lat": c.y, "lon": c.x, "osm_id": "way/999"}})
    assert clash.status_code == 409


def _grouping_handler():
    # Different sizes, so the crawler doesn't drop them as duplicates of each other.
    plans = [draw_plan(900 + 40 * i, 600 + 30 * i)[0] for i in range(4)]
    base = _handler()
    site = (
        "<title>Plans</title>"
        '<h2>Memorial Union</h2><p>800 Langdon St, Madison, WI 53706</p>'
        '<a href="/plans/mu_floor1.png">Memorial Union First Floor</a>'
        '<a href="/plans/mu_floor3.png">Memorial Union Third Floor</a>'
        '<a href="/mystery/floor-plans">Mystery Hall floor plans</a>'
    )
    mystery_page = (
        "<title>Mystery Hall floor plans</title><h2>Mystery Hall</h2>"
        '<a href="/plans/mystery_floor1.png">Mystery Hall First Floor</a>'
        '<a href="/plans/mystery_floor2.png">Mystery Hall Second Floor</a>'
    )
    images = dict(zip(("/plans/mu_floor1.png", "/plans/mu_floor3.png", "/plans/mystery_floor1.png",
                       "/plans/mystery_floor2.png"), plans))

    def handle(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "nominatim.openstreetmap.org" and "mystery" in req.url.params.get("q", "").lower():
            return httpx.Response(200, json=[])
        if host in ("photon.komoot.io", "overpass-api.de") or "photon" in host or "overpass" in host:
            return httpx.Response(200, json={"features": [], "elements": []})
        if host == "uni.example.edu" and path == "/":
            return httpx.Response(200, html=site)
        if host == "uni.example.edu" and path == "/mystery/floor-plans":
            return httpx.Response(200, html=mystery_page)
        if host == "uni.example.edu" and path in images:
            return httpx.Response(200, content=images[path], headers={"content-type": "image/png"})
        return base(req)

    return handle


def test_plans_grouped_into_temp_buildings(client):
    http.set_transport(httpx.MockTransport(_grouping_handler()))
    src = client.post(
        "/api/sources", json={"name": "Campus", "seed_urls": ["https://uni.example.edu/"], "location_hint": "Madison, WI"}
    ).json()
    job_id = client.post(f"/api/sources/{src['id']}/crawl").json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done", job
    assert job["label"] == "Crawling Campus"
    assert job["detail"]["phase"] == "finished"
    assert job["detail"]["pages_read"] >= 1 and job["detail"]["buildings_found"] == job["result"]["buildings"]

    # Mystery Hall can't be found, so it waits in review as one temp building, not two
    # loose plans, and nothing was put on the map for it.
    drafts = client.get("/api/drafts").json()
    mystery = [d for d in drafts if d["name"] == "Mystery Hall"]
    assert len(mystery) == 1, [d["name"] for d in drafts]
    d = mystery[0]
    assert d["match_state"] == "done" and d["suggestion"] is None
    assert sorted(c["level_guess"] for c in d["candidates"]) == [1, 2]
    # Memorial Union was found confidently, so both its floors went straight on the map.
    assert job["result"]["auto_buildings"] == 1 and job["result"]["to_review"] == 1
    names = {b["name"] for b in client.get("/api/buildings").json()}
    assert "Mystery Hall" not in names

    # The user types its address once; confirming makes the building and converts every plan.
    place = {"lat": LAT + 0.01, "lon": LON, "name": "Mystery Hall", "display_name": "1 Mystery Way", "footprint": _footprint()}
    d = client.patch(f"/api/drafts/{d['id']}", json={"address": "1 Mystery Way", "choice": {"place": place}}).json()
    assert d["suggestion"]["chosen"] and d["address"] == "1 Mystery Way"
    r = client.post(f"/api/drafts/{d['id']}/confirm", json={"name": "Mystery Hall"})
    assert r.status_code == 200, r.text
    assert len(r.json()["job_ids"]) == len(d["candidates"])
    b = client.get(f"/api/buildings/{r.json()['building']['id']}").json()
    assert b["name"] == "Mystery Hall" and len(b["floors"]) == len(d["candidates"])
    assert all(x["name"] != "Mystery Hall" for x in client.get("/api/drafts").json())

    act = client.get("/api/activity").json()
    assert not act["running"] and not act["queued"]
    kinds = [j["kind"] for j in act["recent"]]
    assert "crawl" in kinds and "approve_candidate" in kinds
    assert any(j["label"].startswith("Converting Mystery Hall") for j in act["recent"])


def test_move_plan_to_its_own_temp_building(client):
    from floored.db import session_scope
    from floored.models import Candidate, Source

    with session_scope() as s:
        src = Source(name="Campus", seed_urls=["https://uni.example.edu/"], location_hint=None)
        s.add(src)
        s.flush()
        for lvl in (1, 2):
            s.add(Candidate(source_id=src.id, asset_url=f"https://x/{lvl}.png", building_guess="Old Main", level_guess=lvl,
                            status="unmatched", plan_score=0.7))
    drafts = client.get("/api/drafts").json()  # plans from before temp buildings get grouped
    assert len(drafts) == 1 and len(drafts[0]["candidates"]) == 2
    cid = drafts[0]["candidates"][1]["id"]
    moved = client.patch(f"/api/candidates/{cid}", json={"draft_id": "new", "level_guess": 3}).json()
    assert moved["draft_id"] != drafts[0]["id"] and moved["level_guess"] == 3
    assert len(client.get("/api/drafts").json()) == 2
    assert client.post(f"/api/drafts/{drafts[0]['id']}/confirm", json={}).status_code == 409  # no address yet
    assert client.post(f"/api/drafts/{drafts[0]['id']}/reject").json()["rejected"] == 1
    assert len(client.get("/api/drafts").json()) == 1


def test_stop_cancels_a_queued_job(client):
    from floored.db import session_scope
    from floored.models import Job

    with session_scope() as s:
        j = Job(kind="crawl", payload={"source_id": "x"})
        s.add(j)
        s.flush()
        job_id = j.id
    r = client.post(f"/api/jobs/{job_id}/stop").json()
    assert r["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job_id}/stop").status_code == 409
    jobs._run(job_id)  # a cancelled job never runs
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"


def test_everything_is_editable_afterwards(client):
    src = client.post("/api/sources", json={"name": "Campus", "seed_urls": ["https://uni.example.edu/"]}).json()
    src = client.patch(f"/api/sources/{src['id']}", json={"name": "Main campus", "location_hint": "Madison, WI", "max_pages": 50}).json()
    assert (src["name"], src["location_hint"], src["max_pages"]) == ("Main campus", "Madison, WI", 50)
    assert client.patch(f"/api/sources/{src['id']}", json={"seed_urls": ["ftp://x"]}).status_code == 422

    b = client.post("/api/buildings", json={"name": "Union", "place": {"lat": LAT, "lon": LON, "footprint": _footprint()}}).json()
    png, _ = draw_plan()
    floor_id = client.post(f"/api/buildings/{b['id']}/floors", files={"file": ("p.png", png, "image/png")}).json()["floor"]["id"]
    floor = client.patch(f"/api/floors/{floor_id}", json={"name": "Mezzanine", "level": 1.5}).json()
    assert floor["name"] == "Mezzanine" and floor["level"] == 1.5
    room = next(f for f in floor["geojson"]["features"] if f["properties"]["feature"] == "room")
    assert room["properties"]["level"] == 1.5
    floor = client.patch(f"/api/floors/{floor_id}/rooms/{room['properties']['room_id']}", json={"label": "2310"}).json()
    assert any(f["properties"].get("label") == "2310" for f in floor["geojson"]["features"])


def test_merge_rooms(client):
    b = client.post("/api/buildings", json={"name": "Union", "place": {"lat": LAT, "lon": LON, "footprint": _footprint()}}).json()
    png, _ = draw_plan()
    floor_id = client.post(f"/api/buildings/{b['id']}/floors", files={"file": ("p.png", png, "image/png")}).json()["floor"]["id"]
    floor = client.get(f"/api/floors/{floor_id}").json()
    rooms = [f for f in floor["geojson"]["features"] if f["properties"]["feature"] == "room"]
    # Two rooms that share a wall: the pair whose outlines are closest.
    best = min(
        ((a, c) for i, a in enumerate(rooms) for c in rooms[i + 1:]),
        key=lambda p: shape(p[0]["geometry"]).distance(shape(p[1]["geometry"])),
    )
    ids = [r["properties"]["room_id"] for r in best]
    area = sum(r["properties"]["area_m2"] for r in best)
    r = client.post(f"/api/floors/{floor_id}/rooms/merge", json={"room_ids": ids, "label": "Suite 200"})
    assert r.status_code == 200, r.text
    after = [f for f in r.json()["floor"]["geojson"]["features"] if f["properties"]["feature"] == "room"]
    assert len(after) == len(rooms) - 1
    merged = next(f for f in after if f["properties"]["room_id"] == ids[0])
    assert merged["properties"]["label"] == "Suite 200"
    assert merged["geometry"]["type"] == "Polygon"
    assert merged["properties"]["area_m2"] >= area  # includes the wall between them
    assert client.post(f"/api/floors/{floor_id}/rooms/merge", json={"room_ids": [ids[0], "nope"]}).status_code == 404


def test_floors_never_share_a_level(client):
    from floored import pipeline
    from floored.db import session_scope
    from floored.models import Building, Floor

    b = client.post("/api/buildings", json={"name": "Union", "place": {"lat": LAT, "lon": LON, "footprint": _footprint()}}).json()
    png, _ = draw_plan()
    up = lambda lvl: client.post(f"/api/buildings/{b['id']}/floors", files={"file": ("p.png", png, "image/png")},
                                 data={"level": str(lvl)}).json()["floor"]
    first, second = up(1), up(1)
    assert first["level"] == 1 and second["level"] == 2
    assert second["level_note"] and not first["level_note"]

    # Moving a floor onto a taken level swaps the two.
    moved = client.patch(f"/api/floors/{second['id']}", json={"level": 1}).json()
    assert moved["level"] == 1 and moved["level_note"] is None
    assert client.get(f"/api/floors/{first['id']}").json()["level"] == 2

    # Buildings saved before this rule can be straightened out in one go.
    with session_scope() as s:
        for f in s.get(Building, b["id"]).floors:
            f.level = 0
    detail = client.get(f"/api/buildings/{b['id']}").json()
    assert detail["shared_levels"] == 1
    r = client.post(f"/api/buildings/{b['id']}/spread-levels").json()
    assert r["moved"] == 1 and r["building"]["shared_levels"] == 0
    assert sorted(f["level"] for f in r["building"]["floors"]) == [0, 1]
    assert pipeline.free_level({0.0, 1.0}, 1) == 2.0 and pipeline.free_level(set(), None) == 1.0


def test_confirming_plans_with_the_same_level_keeps_them_apart(client):
    from floored import storage
    from floored.db import session_scope
    from floored.models import Candidate, Source

    with session_scope() as s:
        src = Source(name="Campus", seed_urls=["https://uni.example.edu/"])
        s.add(src)
        s.flush()
        for i in range(2):
            png, _ = draw_plan(900 + 40 * i, 600)
            s.add(Candidate(source_id=src.id, asset_url=f"https://x/{i}.png", building_guess="Old Main", level_guess=1,
                            level_label="First Floor", status="unmatched", plan_score=0.7,
                            image_path=storage.save_bytes(png, "candidates", ".png")))
    d = client.get("/api/drafts").json()[0]
    place = {"lat": LAT, "lon": LON, "name": "Old Main", "display_name": "Old Main", "footprint": _footprint()}
    r = client.post(f"/api/drafts/{d['id']}/confirm", json={"place": place})
    assert r.status_code == 200, r.text
    floors = client.get(f"/api/buildings/{r.json()['building']['id']}").json()["floors"]
    assert sorted(f["level"] for f in floors) == [1, 2]
    assert [bool(f["level_note"]) for f in sorted(floors, key=lambda f: f["level"])] == [False, True]


def test_guessed_level_settled_by_the_plan_text(client):
    from floored import pipeline
    from floored.db import session_scope
    from floored.models import Building, Floor

    b = client.post("/api/buildings", json={"name": "Union", "place": {"lat": LAT, "lon": LON}}).json()
    png, _ = draw_plan()
    with session_scope() as s:
        bld = s.get(Building, b["id"])
        first = pipeline.create_floor(s, bld, png, level=1, source_url="a")
        guessed = pipeline.create_floor(s, bld, png, level=None, source_url="b")
        assert guessed.level == 2 and guessed.level_note
        pipeline._floor_from_plan_text(guessed, {"level": 1, "label": "First Floor", "confidence": 0.9})
        assert guessed.level == 2 and "another plan" in guessed.level_note  # 1 is taken: keep it, say why
        pipeline._floor_from_plan_text(guessed, {"level": -1, "label": "Basement", "confidence": 0.9})
        assert (guessed.level, guessed.name, guessed.level_note) == (-1, "Basement", None)
        pipeline._floor_from_plan_text(first, {"level": 5, "confidence": 0.9})
        assert first.level == 1  # a level nobody guessed is left alone
