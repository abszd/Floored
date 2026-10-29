"""Score the converter against synthetic plans with known rooms.

    python bench/benchmark.py --seeds 0-4                 # all styles, 5 plans each
    python bench/benchmark.py --styles cad,scan --legacy  # also score the old converter
    python bench/benchmark.py --seeds 0-9 --json out.json

For every plan, predicted rooms are rasterised and matched one-to-one to the true
rooms by IoU (Hungarian matching). Reported per style:

* ``F1``: a room counts as found when it is matched with IoU >= 0.5.
* ``mIoU``: mean IoU of every true room with its match (0 when unmatched).
* ``split``: share of true rooms cut into two or more predicted rooms (each holding
  at least 25% of it), the old converter's big-room problem.
* ``merge``: predicted rooms that swallowed two or more true rooms (at least half of each).
* ``outline``: IoU of the building outline with the true footprint.

Doorway pixels are left out of the scoring, since either side may claim them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

os.environ.setdefault("OMP_NUM_THREADS", "1")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from scipy.optimize import linear_sum_assignment  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from floorplan_converter import convert, synthetic  # noqa: E402


def rasterize(polys, shape) -> np.ndarray:
    """Label image from (outer, holes) polygons in corner-convention pixel coordinates."""
    out = np.zeros(shape, np.int32)
    for k, (outer, holes) in enumerate(polys, start=1):
        m = np.zeros(shape, np.uint8)
        cv2.fillPoly(m, [_ring(outer)], 1)
        for h in holes:
            cv2.fillPoly(m, [_ring(h)], 0)
        out[m > 0] = k
    return out


def _ring(pts):
    # Corner convention: pixel (i, j) spans [j, j+1] x [i, i+1], and fillPoly fills the
    # pixels on the ring, so pull the vertices in by half a pixel.
    return np.round(np.asarray(pts, np.float64) - 0.5).astype(np.int32)


def score(gt: np.ndarray, pred: np.ndarray) -> dict:
    valid = gt >= 0
    g = np.where(valid, gt, 0)
    p = np.where(valid, pred, 0)
    gids = [int(v) for v in np.unique(g) if v > 0]
    pids = [int(v) for v in np.unique(p) if v > 0]
    if not gids:
        return {}
    gi = {v: i for i, v in enumerate(gids)}
    pi = {v: i for i, v in enumerate(pids)}
    garea = np.array([(g == v).sum() for v in gids], np.float64)
    parea = np.array([(p == v).sum() for v in pids], np.float64)
    inter = np.zeros((len(gids), max(1, len(pids))))
    both = (g > 0) & (p > 0)
    if both.any() and pids:
        pairs, counts = np.unique(np.stack([g[both], p[both]]), axis=1, return_counts=True)
        for (a, b), c in zip(pairs.T, counts):
            inter[gi[int(a)], pi[int(b)]] = c
    if pids:
        union = garea[:, None] + parea[None, :] - inter
        iou = inter / np.maximum(union, 1)
        r, c = linear_sum_assignment(-iou)
        matched = iou[r, c]
        best = np.zeros(len(gids))
        best[r] = matched
        tp = int((matched >= 0.5).sum())
        frac_g = inter / garea[:, None]  # share of each true room inside each predicted room
        split = int(((frac_g >= 0.25).sum(axis=1) >= 2).sum())
        merge = int(((frac_g >= 0.5).sum(axis=0) >= 2).sum())
    else:
        best = np.zeros(len(gids))
        tp = split = merge = 0
    prec = tp / max(1, len(pids))
    rec = tp / len(gids)
    return {
        "n_gt": len(gids),
        "n_pred": len(pids),
        "tp": tp,
        "precision": prec,
        "recall": rec,
        "f1": 2 * prec * rec / max(1e-9, prec + rec),
        "miou": float(best.mean()),
        "split": split,
        "merge": merge,
    }


def outline_iou(footprint_gt: np.ndarray, footprint_parts) -> float:
    pred = rasterize([(f["polygon"], f["holes"]) for f in footprint_parts], footprint_gt.shape) > 0
    gt = footprint_gt > 0
    return float((pred & gt).sum() / max(1, (pred | gt).sum()))


def run_one(job):
    style, seed, legacy, ocr = job
    plan = synthetic.generate(seed, style)
    t = time.perf_counter()
    res = convert(plan.image, ocr=ocr)
    dt = time.perf_counter() - t
    pred = rasterize([(r.polygon, r.holes) for r in res.rooms if r.kind != "outdoor"], plan.labels.shape)
    row = {"style": style, "seed": seed, "time_s": dt, **score(plan.labels, pred),
           "outline": outline_iou(plan.footprint, res.footprint)}
    if legacy:
        from legacy import legacy_labels

        t = time.perf_counter()
        try:
            lab = legacy_labels(plan.image, ocr)
        except Exception:  # the old pipeline raises when it finds no plan
            lab = np.zeros(plan.labels.shape, np.int32)
        row["legacy"] = {"time_s": time.perf_counter() - t, **score(plan.labels, lab)}
    return row


def _seeds(spec: str):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def summarize(rows, key=None):
    def get(r):
        return r[key] if key else r

    rows = [r for r in rows if get(r)]
    if not rows:
        return {}
    n_gt = sum(get(r)["n_gt"] for r in rows)
    return {
        "plans": len(rows),
        "f1": float(np.mean([get(r)["f1"] for r in rows])),
        "miou": float(np.mean([get(r)["miou"] for r in rows])),
        "split": sum(get(r)["split"] for r in rows) / max(1, n_gt),
        "merge": sum(get(r)["merge"] for r in rows) / max(1, n_gt),
        "time_s": float(np.mean([get(r)["time_s"] for r in rows])),
        **({"outline": float(np.mean([r["outline"] for r in rows]))} if not key else {}),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--styles", default=",".join(synthetic.STYLES))
    ap.add_argument("--seeds", default="0-2")
    ap.add_argument("--legacy", action="store_true", help="also score the old watershed converter")
    ap.add_argument("--ocr", default="auto")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--json", help="write per-plan rows to this file")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every plan")
    args = ap.parse_args(argv)

    jobs = [(s, k, args.legacy, args.ocr) for s in args.styles.split(",") for k in _seeds(args.seeds)]
    if args.workers > 1:
        with ProcessPoolExecutor(args.workers) as ex:
            rows = list(ex.map(run_one, jobs))
    else:
        rows = [run_one(j) for j in jobs]

    if args.verbose:
        for r in rows:
            print(f"{r['style']:11s} seed {r['seed']:3d}  rooms {r['n_pred']:3d}/{r['n_gt']:3d}  F1 {r['f1']:.2f}  "
                  f"mIoU {r['miou']:.2f}  split {r['split']}  merge {r['merge']}  outline {r['outline']:.3f}  {r['time_s']:.1f}s")
    hdr = f"{'style':11s} {'plans':>5s} {'F1':>6s} {'mIoU':>6s} {'split':>6s} {'merge':>6s} {'outline':>8s} {'time':>6s}"
    if args.legacy:
        hdr += f" | {'old F1':>6s} {'old mIoU':>8s} {'old split':>9s} {'old merge':>9s}"
    print(hdr)
    styles = list(dict.fromkeys(r["style"] for r in rows))
    for st in styles + ["all"]:
        sel = [r for r in rows if st == "all" or r["style"] == st]
        s = summarize(sel)
        line = (f"{st:11s} {s['plans']:5d} {s['f1']:6.3f} {s['miou']:6.3f} {s['split']:6.3f} {s['merge']:6.3f} "
                f"{s['outline']:8.3f} {s['time_s']:5.1f}s")
        if args.legacy:
            o = summarize(sel, "legacy")
            line += f" | {o['f1']:6.3f} {o['miou']:8.3f} {o['split']:9.3f} {o['merge']:9.3f}"
        print(line)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
