"""Hand edits to converted plans: crop, split a sheet into floors, split and draw rooms, undo."""

import cv2
import numpy as np
from shapely.geometry import shape

from synth import draw_plan
from test_api import LAT, LON, _footprint, client  # noqa: F401  (fixture)


def _sheet(*, title_block: bool = False) -> bytes:
    """Two copies of the synthetic plan stacked on one sheet, or one plan beside a title block."""
    a = cv2.imdecode(np.frombuffer(draw_plan()[0], np.uint8), cv2.IMREAD_COLOR)
    if title_block:
        block = np.full((600, 500, 3), 255, np.uint8)
        cv2.rectangle(block, (40, 40), (460, 560), (0, 0, 0), 6)
        cv2.putText(block, "MEMORIAL UNION", (60, 300), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
        img = np.hstack([a, block])
    else:
        img = np.vstack([a, a])
    return cv2.imencode(".png", img)[1].tobytes()


def _upload(client, png, level=1):
    b = client.post("/api/buildings", json={"name": "Union", "place": {"lat": LAT, "lon": LON, "footprint": _footprint()}}).json()
    f = client.post(
        f"/api/buildings/{b['id']}/floors", files={"file": ("p.png", png, "image/png")}, data={"level": str(level)}
    ).json()["floor"]
    return b, client.get(f"/api/floors/{f['id']}").json()


def _rooms(floor):
    return [f for f in floor["geojson"]["features"] if f["properties"]["feature"] == "room"]


def test_split_a_sheet_into_two_floors_and_undo(client):
    b, floor = _upload(client, _sheet())
    assert floor["image_size"] == [900, 1200]
    before = len(_rooms(floor))
    # A room number fixed before splitting survives the split.
    top_room = _rooms(floor)[0]
    client.patch(f"/api/floors/{floor['id']}/rooms/{top_room['properties']['room_id']}", json={"label": "101A"})

    r = client.post(
        f"/api/floors/{floor['id']}/split",
        json={"regions": [{"bbox": [0, 0, 900, 600], "level": 1}, {"bbox": [0, 600, 900, 1200], "level": 2, "name": "Second"}]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    first, second = out["floors"]
    assert first["id"] == floor["id"] and first["level"] == 1
    assert second["level"] == 2 and second["name"] == "Second"
    top = client.get(f"/api/floors/{first['id']}").json()
    bottom = client.get(f"/api/floors/{second['id']}").json()
    assert top["image_size"] == [900, 600] and bottom["image_size"] == [900, 600]
    # The converter only read the bigger drawing on the sheet; that part keeps its rooms and
    # their edits, and the part it missed is converted on its own.
    assert len(_rooms(bottom)) == before
    assert "101A" in {r["properties"]["label"] for r in _rooms(bottom)}
    assert out["job_ids"] and len(_rooms(top)) == before
    # Each half is fitted to the building on its own.
    assert top["fit_score"] > 0.8 and bottom["fit_score"] > 0.8
    assert top["undo"] == "Split into 2 floors"

    u = client.post(f"/api/floors/{floor['id']}/undo")
    assert u.status_code == 200 and u.json()["undone"] == "Split into 2 floors"
    restored = u.json()["floor"]
    assert restored["image_size"] == [900, 1200] and len(_rooms(restored)) == before
    assert client.get(f"/api/floors/{second['id']}").status_code == 404
    # The rename before it is next in line.
    assert restored["undo"] == "Rename room"


def test_crop_off_a_title_block(client):
    _, floor = _upload(client, _sheet(title_block=True))
    r = client.post(f"/api/floors/{floor['id']}/split", json={"regions": [{"bbox": [0, 0, 900, 600]}], "refit": True})
    assert r.status_code == 200, r.text
    cropped = r.json()["floor"]
    assert cropped["image_size"] == [900, 600]
    assert cropped["level"] == 1
    assert cropped["fit_score"] > 0.8
    # Without re-fitting, the cropped plan stays exactly where it was on the map.
    room = _rooms(cropped)[0]
    again = client.post(
        f"/api/floors/{floor['id']}/split", json={"regions": [{"bbox": [10, 10, 890, 590]}], "refit": False}
    ).json()["floor"]
    same = next(r for r in _rooms(again) if r["properties"]["room_id"] == room["properties"]["room_id"])
    assert shape(same["geometry"]).symmetric_difference(shape(room["geometry"])).area < 0.02 * shape(room["geometry"]).area


def test_split_and_draw_rooms(client):
    _, floor = _upload(client, draw_plan()[0])
    rooms = _rooms(floor)
    big = max(rooms, key=lambda r: r["properties"]["area_m2"])
    rid = big["properties"]["room_id"]
    # A line across the room on the map, through its middle.
    g = shape(big["geometry"])
    minx, miny, maxx, maxy = g.bounds
    cx = (minx + maxx) / 2
    line = [[cx, miny + (maxy - miny) * 0.02], [cx, maxy - (maxy - miny) * 0.02]]
    r = client.post(f"/api/floors/{floor['id']}/rooms/{rid}/split", json={"line": line})
    assert r.status_code == 200, r.text
    ids = r.json()["room_ids"]
    assert ids[0] == rid and len(ids) == 2
    after = _rooms(r.json()["floor"])
    assert len(after) == len(rooms) + 1
    parts = [shape(f["geometry"]).area for f in after if f["properties"]["room_id"] in ids]
    assert abs(sum(parts) - g.area) < 0.05 * g.area

    miss = client.post(f"/api/floors/{floor['id']}/rooms/{rid}/split", json={"line": [[0, 0], [5, 5]], "pixels": True})
    assert miss.status_code == 422

    # Draw a room in pixels over part of the corridor.
    r = client.post(
        f"/api/floors/{floor['id']}/rooms",
        json={"ring": [[100, 270], [200, 270], [200, 330], [100, 330]], "pixels": True, "label": "Kiosk", "kind": "room"},
    )
    assert r.status_code == 201, r.text
    labels = {f["properties"]["label"] for f in _rooms(r.json()["floor"])}
    assert "Kiosk" in labels
    assert r.json()["floor"]["undo"] == "Draw room"
    back = client.post(f"/api/floors/{floor['id']}/undo").json()["floor"]
    assert "Kiosk" not in {f["properties"]["label"] for f in _rooms(back)}


def test_copy_floor_and_line_up_all(client):
    b, floor = _upload(client, draw_plan()[0])
    copy = client.post(f"/api/floors/{floor['id']}/duplicate", json={}).json()
    assert copy["level"] == 2 and len(_rooms(copy)) == len(_rooms(floor))
    third = client.post(f"/api/floors/{floor['id']}/duplicate", json={"level": 2}).json()
    assert third["level"] == 3 and third["level_note"]

    p = dict(floor["placement"], rotation_deg=floor["placement"]["rotation_deg"] + 10)
    client.put(f"/api/floors/{floor['id']}/placement", json=p)
    assert client.post(f"/api/floors/{floor['id']}/line-up-all").json()["moved"] == 2
    assert client.get(f"/api/floors/{copy['id']}").json()["placement"]["rotation_deg"] == p["rotation_deg"]
    assert client.get(f"/api/floors/{copy['id']}").json()["undo"] == f"Line up with {floor['name']}"


def test_sheets_the_converter_flags(client):
    from floored import pipeline
    from floored.db import session_scope
    from floored.models import Floor

    _, floor = _upload(client, _sheet())
    sheets = [
        {"bbox": [0, 0, 900, 600], "level": 1, "label": "First floor", "confidence": 0.4},
        {"bbox": [0, 600, 900, 1200], "level": 2, "label": "Second floor", "confidence": 0.4},
    ]
    with session_scope() as s:
        f = s.get(Floor, floor["id"])
        f.conversion = {**f.conversion, "meta": {**f.conversion["meta"], "sheets": sheets}}
        assert pipeline.auto_split(s, f) == []  # unsure: only suggested
    summary = client.get(f"/api/floors/{floor['id']}").json()
    assert summary["sheet_count"] == 2 and len(summary["sheets"]) == 2

    with session_scope() as s:
        f = s.get(Floor, floor["id"])
        f.conversion = {**f.conversion, "meta": {**f.conversion["meta"], "sheets": [dict(x, confidence=0.9) for x in sheets]}}
        floors = pipeline.auto_split(s, f)
        assert [x.level for x in floors] == [1, 2]
        assert floors[1].name == "Second floor"
    after = client.get(f"/api/floors/{floor['id']}").json()
    assert after["sheet_count"] == 0 and after["image_size"] == [900, 600]


def test_remove_an_area_of_rooms_and_walls(client):
    _, floor = _upload(client, draw_plan()[0])
    rooms = _rooms(floor)
    walls = [f for f in floor["geojson"]["features"] if f["properties"]["feature"] == "wall"]
    ids = [r["properties"]["room_id"] for r in rooms[:2]]
    r = client.post(f"/api/floors/{floor['id']}/delete", json={"room_ids": ids, "walls": [0, 1] if len(walls) > 1 else []})
    assert r.status_code == 200, r.text
    assert r.json()["removed"]["rooms"] == 2
    assert len(_rooms(r.json()["floor"])) == len(rooms) - 2
    assert r.json()["floor"]["undo"].startswith("Remove 2 rooms")
    assert client.post(f"/api/floors/{floor['id']}/delete", json={"room_ids": ["nope"]}).status_code == 404
    back = client.post(f"/api/floors/{floor['id']}/undo").json()["floor"]
    assert len(_rooms(back)) == len(rooms)


def test_fixes_are_logged_and_exported(client, tmp_path):
    import json

    from floored import feedback

    _, floor = _upload(client, _sheet())
    fid = floor["id"]
    room = _rooms(floor)[0]["properties"]["room_id"]
    client.patch(f"/api/floors/{fid}/rooms/{room}", json={"label": "101", "kind": "restroom"})
    out = client.post(
        f"/api/floors/{fid}/split",
        json={"regions": [{"bbox": [0, 600, 900, 1200], "level": 1}, {"bbox": [0, 0, 900, 600], "level": 2}]},
    ).json()
    child = out["floors"][1]["id"]
    p = client.get(f"/api/floors/{fid}").json()["placement"]
    client.put(f"/api/floors/{fid}/placement", json={**p, "rotation_deg": p["rotation_deg"] + 3})

    index = feedback.export(tmp_path / "fb")
    by_id = {e["floor_id"]: e for e in index["floors"]}
    assert set(by_id[fid]["ops"]) == {"rename", "set_kind", "split_floor", "move"}

    d = tmp_path / "fb" / fid
    machine = json.loads((d / "machine.json").read_text())
    final = json.loads((d / "final.json").read_text())
    # The converter's output is the whole sheet, untouched by the fixes.
    assert machine["image_size"] == [900, 1200] and (d / "image.png").exists()
    assert len(machine["conversion"]["rooms"]) == 7 and machine["placement"]
    # The floor now is the bottom half of it.
    assert final["image_size"] == [900, 600] and (d / "final.png").exists()
    assert final["origin"] == {"floor": fid, "box": [0, 600, 900, 1200]}
    assert final["building"]["footprint"]["type"] == "Polygon"
    edits = [json.loads(line) for line in (d / "edits.jsonl").read_text().splitlines()]
    assert [e["op"] for e in edits] == ["rename", "set_kind", "split_floor", "move"]
    assert edits[0]["params"] == {"room_id": room, "before": None, "after": "101"}

    # The top half had no rooms, so it was converted on its own: its machine output is its own.
    assert by_id.get(child) is None or by_id[child]["machine_floor"] == child

    # A part split off with its rooms has no machine output of its own: it points at the sheet's.
    out = client.post(
        f"/api/floors/{fid}/split", json={"regions": [{"bbox": [0, 0, 450, 600]}, {"bbox": [450, 0, 900, 600]}]}
    ).json()
    right = out["floors"][1]["id"]
    index = feedback.export(tmp_path / "fb2")
    entry = next(e for e in index["floors"] if e["floor_id"] == right)
    assert entry["machine_floor"] == fid
    final = json.loads((tmp_path / "fb2" / right / "final.json").read_text())
    assert final["origin"] == {"floor": fid, "box": [450, 600, 900, 1200]}
    left = json.loads((tmp_path / "fb2" / fid / "final.json").read_text())
    assert left["origin"]["box"] == [0, 600, 450, 1200]


def test_crop_other_pages_of_the_same_file_alike(client):
    from floored.db import session_scope
    from floored.models import Floor

    b, first = _upload(client, _sheet(title_block=True))
    second = client.post(
        f"/api/buildings/{b['id']}/floors", files={"file": ("p.png", _sheet(title_block=True), "image/png")}, data={"level": "2"}
    ).json()["floor"]
    with session_scope() as s:
        s.get(Floor, first["id"]).source_url = "https://x.edu/plans.pdf"
        s.get(Floor, second["id"]).source_url = "https://x.edu/plans.pdf#page=2"
    assert client.get(f"/api/floors/{first['id']}").json()["same_layout"] == [second["id"]]
    r = client.post(
        f"/api/floors/{first['id']}/split", json={"regions": [{"bbox": [0, 0, 900, 600]}], "also": [second["id"]]}
    ).json()
    assert r["also_cropped"] == [second["id"]]
    assert client.get(f"/api/floors/{second['id']}").json()["image_size"] == [900, 600]
    assert client.get(f"/api/floors/{second['id']}").json()["undo"] == "Crop like another page"
