"""Finding the building on a sheet, telling floors apart, and colouring inside rooms."""

import cv2
import numpy as np
from benchmark import rasterize, score
from sheets import make, ring_iou

from floorplan_converter import convert, detect_sheets, layout, rooms, synthetic
from floorplan_converter.text import TextItem


def _box_iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union


def test_heavy_title_is_not_the_building():
    # A small plan under the building's name in very large heavy type: the name used
    # to be taken for the building.
    sh = make(3, "big_title")
    res = convert(sh.image, read_floor=False)
    assert ring_iou(res.outline, sh.plans[0]["footprint"]) >= 0.9
    _, y0, _, y1 = sh.plans[0]["bbox"]
    for part in res.footprint:  # nothing of the title kept as a wing
        pts = np.asarray(part["polygon"])
        assert pts[:, 1].min() >= y0 - 0.1 * (y1 - y0)


def test_page_frame_and_title_block_are_not_the_building():
    sh = make(3, "title_block")
    res = convert(sh.image, read_floor=False)
    assert ring_iou(res.outline, sh.plans[0]["footprint"]) >= 0.9


def test_two_floors_on_one_sheet_come_back_as_two():
    sh = make(3, "two_floors")
    res = convert(sh.image)
    sheets = res.meta["sheets"]
    assert [s["level"] for s in sheets] == [p["floor"] for p in sh.plans]
    for s, p in zip(sheets, sh.plans):
        assert ring_iou(s["outline"], p["footprint"]) >= 0.9
        assert s["confidence"] >= 0.5 and s["floor"]["level"] == s["level"]
        assert _box_iou(s["bbox"], p["bbox"]) >= 0.8
    # Each floor's rooms lie in its own part of the sheet.
    boxes = [s["bbox"] for s in sheets]
    for r in res.rooms:
        cx, cy = np.asarray(r.polygon).mean(axis=0)
        assert sum(b[0] - 5 <= cx <= b[2] + 5 and b[1] - 5 <= cy <= b[3] + 5 for b in boxes) == 1
    assert res.outline == max((s["outline"] for s in sheets), key=lambda o: cv2.contourArea(np.float32(o)))


def test_detect_sheets():
    sh = make(3, "two_floors")
    found = detect_sheets(sh.image)
    assert [s["level"] for s in found] == [p["floor"] for p in sh.plans]
    for s, p in zip(found, sh.plans):
        assert _box_iou(s["bbox"], p["bbox"]) >= 0.8
        assert s["outline"] is None
    assert detect_sheets(make(1, "title_block").image) is None


def _group(k, bbox):
    return layout.Group(k, bbox, length=400.0, fill=8000.0, weight=3.0)


def _item(text, x, y):
    return TextItem(np.array([[x, y], [x + 80, y], [x + 80, y + 12], [x, y + 12]], np.float64), text, 0.95)


def test_wings_stay_together_and_floors_split():
    lay = layout.Layout(np.zeros((300, 600), np.int32), {1: _group(1, (0, 0, 200, 200)), 2: _group(2, (380, 0, 580, 200))}, 1)
    same = [_item("THIRD FLOOR", 50, 220), _item("THIRD FLOOR", 430, 220)]
    plans = layout._split_floors(lay, [1, 2], same)
    assert len(plans) == 1 and plans[0].bbox == (0, 0, 580, 200)

    other = [_item("FIRST FLOOR PLAN", 50, 220), _item("SECOND FLOOR PLAN", 430, 220)]
    plans = layout._split_floors(lay, [1, 2], other)
    assert sorted(p.floor["level"] for p in plans) == [1.0, 2.0]
    assert all(p.confidence >= 0.5 for p in plans)

    # Nothing to read and different shapes: one building.
    assert len(layout._split_floors(lay, [1, 2], [])) == 1


def _draw_building(frame_gap=None):
    m = np.zeros((400, 600), np.uint8)
    cv2.rectangle(m, (50, 50), (550, 350), 1, 6)  # outer wall
    cv2.line(m, (300, 50), (300, 350), 1, 4)  # inner walls run up to it
    cv2.line(m, (50, 200), (300, 200), 1, 4)
    cv2.line(m, (380, 120), (480, 120), 1, 3)  # a free-standing partition
    cv2.line(m, (380, 120), (380, 280), 1, 3)
    return m.astype(bool)


def test_rectangular_building_is_not_a_page_frame():
    m = _draw_building()
    assert (layout.remove_frames(m) == m).all()


def test_page_frame_with_title_block_is_removed():
    m = np.zeros((700, 1000), np.uint8)
    cv2.rectangle(m, (10, 10), (989, 689), 1, 4)  # page frame
    cv2.line(m, (760, 10), (760, 689), 1, 4)  # title block on the right
    for y in (150, 300, 450, 600):
        cv2.line(m, (760, y), (989, y), 1, 4)
    sub = _draw_building()[30:370, 30:570]
    m = m.astype(bool)
    m[150:490, 100:640] |= sub
    out = layout.remove_frames(m)
    assert not out[:, 700:].any()  # frame and title block gone
    assert (out[150:490, 100:640] == sub).all()  # the drawing kept


def test_rooms_split_only_by_a_pale_line_are_joined():
    labels = np.zeros((120, 200), np.int32)
    labels[10:59, 10:190] = 1
    labels[61:110, 10:100] = 2
    labels[61:110, 102:190] = 3
    barrier = labels == 0
    pale = np.zeros_like(barrier)
    pale[59:61, 10:100] = True  # pale line between 1 and 2; a dark wall between 1 and 3
    out = rooms.merge_pale(labels, barrier, pale, min_len=20)
    assert out[30, 50] == out[80, 50]
    assert out[30, 50] != out[80, 150]
    # A short pale stretch may be a doorway with its threshold drawn: kept apart.
    assert rooms.merge_pale(labels, barrier, pale, min_len=150)[80, 50] == 2


def test_colouring_inside_rooms_does_not_split_them():
    plan = synthetic.generate(4, "solid")
    painted = synthetic.add_colouring(plan, 4)
    assert painted.meta["colouring"]
    scores = []
    for p in (plan, painted):
        res = convert(p.image, read_floor=False)
        pred = rasterize([(r.polygon, r.holes) for r in res.rooms if r.kind != "outdoor"], p.labels.shape)
        scores.append(score(p.labels, pred))
    assert scores[1]["split"] <= scores[0]["split"]
    assert scores[1]["f1"] >= scores[0]["f1"] - 0.05
