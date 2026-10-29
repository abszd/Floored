"""End-to-end checks on synthetic plans with known rooms (see bench/benchmark.py)."""

import pytest

from benchmark import outline_iou, rasterize, score
from floorplan_converter import convert, synthetic

CASES = [("cad", 1), ("color_fill", 1), ("hatched", 1), ("scan", 1)]


@pytest.mark.parametrize("style,seed", CASES)
def test_rooms_match_ground_truth(style, seed):
    plan = synthetic.generate(seed, style)
    res = convert(plan.image)
    pred = rasterize([(r.polygon, r.holes) for r in res.rooms if r.kind != "outdoor"], plan.labels.shape)
    s = score(plan.labels, pred)
    assert s["f1"] >= 0.75, s
    assert s["miou"] >= 0.8, s
    # Big rooms must not be cut up (the old converter's main failure).
    assert s["split"] <= 1, s
    assert outline_iou(plan.footprint, res.footprint) >= 0.95


def test_room_labels_are_read():
    plan = synthetic.generate(1, "cad")
    res = convert(plan.image)
    labels = " ".join(r.label or "" for r in res.rooms).lower()
    truth = [r.name.lower() for r in plan.rooms if r.name]
    assert len(truth) >= 10
    found = sum(1 for name in truth if name.split()[0] in labels)
    assert found >= 0.6 * len(truth), (found, len(truth))
