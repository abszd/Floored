"""bench/feedback.py reads the editor's export and scores against people's fixes."""

import json

import cv2
import numpy as np
from feedback import evaluate, main, placement_error

from floorplan_converter import synthetic


def _rooms(labels, dx=0, dy=0):
    out = []
    for rid in [int(v) for v in np.unique(labels) if v > 0]:
        cnts, _ = cv2.findContours((labels == rid).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(cnts, key=cv2.contourArea).reshape(-1, 2).astype(float) - (dx, dy)
        out.append({"id": f"r{rid}", "polygon": c.tolist(), "holes": [], "kind": "room"})
    return out


def _export(tmp_path, crop):
    plan = synthetic.generate(3, "solid")
    h, w = plan.labels.shape
    d = tmp_path / "f1"
    d.mkdir()
    cv2.imwrite(str(d / "image.png"), plan.image)
    place = {"center_lon": -89.4, "center_lat": 43.07, "width_m": w / plan.px_per_m, "height_m": h / plan.px_per_m,
             "rotation_deg": 10.0, "mirror": False}
    labels = np.where(plan.labels > 0, plan.labels, 0)
    x0, y0, x1, y1 = crop
    # The machine ran two rooms together; people split them (and cropped the plan).
    merged = labels.copy()
    a, b = [r.id for r in plan.rooms[:2]]
    merged[merged == b] = a
    (d / "machine.json").write_text(json.dumps({
        "conversion": {"image_size": [w, h], "rooms": _rooms(merged), "outline": None},
        "placement": place, "image": "image.png", "image_size": [w, h]}))
    moved = {**place, "center_lon": place["center_lon"] + 2e-5}
    (d / "final.json").write_text(json.dumps({
        "floor_id": "f1", "image_size": [x1 - x0, y1 - y0], "origin": {"floor": "f1", "box": list(crop)},
        "conversion": {"image_size": [x1 - x0, y1 - y0], "rooms": _rooms(labels, x0, y0), "outline": None},
        "placement": moved, "building": {"footprint": None}}))
    (d / "edits.jsonl").write_text("\n".join(json.dumps({"op": op, "params": {}}) for op in ("crop", "split_room", "move")))
    (tmp_path / "index.json").write_text(json.dumps({"floors": [{"floor_id": "f1"}], "matches": []}))
    return plan


def test_scores_the_output_then_against_the_fixed_floor(tmp_path):
    plan = _export(tmp_path, (0, 0, *plan_size()))
    row = evaluate(tmp_path, tmp_path / "f1", rerun=False)
    n = len(plan.rooms)
    assert row["rooms"] == n
    assert row["then"]["tp"] == n - 1 and row["then"]["f1"] < 1
    assert row["ops"]["split_room"] == 1
    assert 0 < row["placement_then_m"] < 5


def test_crops_are_mapped_back_onto_the_image(tmp_path):
    w, h = plan_size()
    _export(tmp_path, (40, 30, w, h))
    row = evaluate(tmp_path, tmp_path / "f1", rerun=False)
    assert row["then"]["miou"] > 0.8


def test_report_runs(tmp_path, capsys):
    _export(tmp_path, (0, 0, *plan_size()))
    main([str(tmp_path), "--no-rerun"])
    out = capsys.readouterr().out
    assert "split_room" in out and "Placement" in out


def test_placement_error_is_zero_for_the_same_placement():
    p = {"center_lon": 10.0, "center_lat": 50.0, "width_m": 40.0, "height_m": 30.0, "rotation_deg": 30.0}
    ring = [[0, 0], [100, 0], [100, 80]]
    assert placement_error(p, (100, 80), p, (100, 80), ring) < 1e-6
    # The same placement of a crop: points move into the full image by the offset.
    crop = {**p, "width_m": 20.0, "height_m": 15.0}
    full_c = _centre_of(p, (100, 80), (50, 40, 100, 80))
    crop.update(center_lon=full_c[0], center_lat=full_c[1])
    assert placement_error(p, (100, 80), crop, (50, 40), [[0, 0], [50, 40]], (50, 40)) < 0.01


def _centre_of(p, size, box):
    from feedback import _pixel_to_lonlat

    return _pixel_to_lonlat(p, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, *size)


def plan_size():
    h, w = synthetic.generate(3, "solid").labels.shape
    return w, h
