"""The wall skeleton as a graph of branches, each with a measured stroke width.

Measuring width per branch as (ink area owned by the branch) / (branch length),
with anti-aliased pixels counted fractionally, resolves line weights to a
fraction of a pixel. That matters: in many plans walls are 4 px and annotation
lines 2-3 px, and a per-pixel distance transform cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from . import skeleton as skel

_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
_PREF = (1, 3, 4, 6, 0, 2, 5, 7)  # indices into _OFFSETS: 4-neighbours, then diagonals


@dataclass
class Branch:
    pts: np.ndarray  # (N, 2) float (x, y), ordered from end a to end b
    a_free: bool  # end a is a free endpoint (not a junction)
    b_free: bool
    width: float = 0.0
    length: float = 0.0

    @property
    def dangling(self) -> bool:
        return self.a_free != self.b_free

    @property
    def isolated(self) -> bool:
        return self.a_free and self.b_free


@dataclass
class Graph:
    branches: list
    skeleton: np.ndarray  # bool
    owner: np.ndarray  # int32 map: branch index owning each mask pixel, -1 none, -2 junction


def build(mask: np.ndarray, weight: np.ndarray = None, prune_len: int = 0, split_ratio: float = 1.6) -> Graph:
    """Skeletonize ``mask``, split the skeleton into branches and measure their widths.

    ``weight`` (float in [0, 1]) is each pixel's ink coverage; defaults to 1.
    Branches are also cut where the stroke width changes by ``split_ratio`` or more:
    a door swing or leaf drawn on from the end of a wall is one skeleton path with it,
    but a different line.
    """
    sk = skel.skeletonize(mask)
    if prune_len > 0:
        sk = skel.prune(sk, prune_len)
    branches, pix_branch = _trace(sk)
    if split_ratio > 0 and branches:
        branches, pix_branch = _split_by_width(branches, pix_branch, mask, weight, split_ratio)
    owner = np.full(mask.shape, -1, np.int32)
    if not branches:
        return Graph(branches, sk, owner)
    _, labels = cv2.distanceTransformWithLabels((~sk).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    ys, xs = np.nonzero(sk)
    lab2b = np.full(len(ys) + 1, -1, np.int32)
    lab2b[1:] = pix_branch[ys, xs]
    owner = np.where(mask, lab2b[labels], -1).astype(np.int32)
    w = np.ones(mask.shape, np.float32) if weight is None else weight.astype(np.float32)
    valid = owner >= 0
    area = np.bincount(owner[valid], weights=w[valid], minlength=len(branches))
    for i, b in enumerate(branches):
        d = np.diff(b.pts, axis=0)
        b.length = float(np.hypot(d[:, 0], d[:, 1]).sum()) if len(b.pts) > 1 else 0.0
        b.width = float(area[i] / max(b.length, 1.0))
    return Graph(branches, sk, owner)


def _split_by_width(branches: list, pix_branch: np.ndarray, mask: np.ndarray, weight, ratio: float):
    """Cut branches where the local stroke width steps up or down by ``ratio``.

    The local width is the ink (coverage-weighted) within a small disk around each
    skeleton pixel divided by the disk's diameter. Unlike a distance transform this
    survives blur and anti-aliasing, which spread a line without adding ink.
    """
    from scipy.ndimage import median_filter

    r = 4
    disk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)).astype(np.float32)
    ink = (weight if weight is not None else mask).astype(np.float32) * mask
    local = cv2.filter2D(ink, -1, disk, borderType=cv2.BORDER_CONSTANT) / (2 * r + 1)
    out = []
    lr = np.log(ratio)
    for i, b in enumerate(branches):
        n = len(b.pts)
        if n < 12:
            out.append(b)
            continue
        xs = b.pts[:, 0].astype(int)
        ys = b.pts[:, 1].astype(int)
        w = np.maximum(local[ys, xs], 0.3)
        k = int(np.clip(2.0 * np.median(w), 5, 12))
        if n < 3 * k:
            out.append(b)
            continue
        lw = np.log(median_filter(w, size=3, mode="nearest"))
        c = np.concatenate([[0.0], np.cumsum(lw)])
        idx = np.arange(k, n - k + 1)
        left = (c[idx] - c[idx - k]) / k  # mean log width over the k pixels before idx
        right = (c[idx + k] - c[idx]) / k
        step = np.abs(right - left)
        cuts = []
        for j in np.argsort(-step):
            if step[j] < lr:
                break
            pos = int(idx[j])
            if all(abs(pos - q) >= k for q in cuts):
                cuts.append(pos)
        if not cuts:
            out.append(b)
            continue
        cuts.sort()
        bounds = [0] + cuts + [n - 1]
        for a, e in zip(bounds[:-1], bounds[1:]):
            seg = b.pts[a:e + 1]
            nb = Branch(seg, b.a_free if a == 0 else False, b.b_free if e == n - 1 else False)
            idx_new = len(out)
            sx, sy = seg[:, 0].astype(int), seg[:, 1].astype(int)
            own = pix_branch[sy, sx] == i
            pix_branch[sy[own], sx[own]] = -(idx_new + 10)  # temporary, re-indexed below
            out.append(nb)
        # Pixels not re-labelled keep the old index; mark them so they are remapped.
    # Re-index: unchanged branches keep their position in ``out``.
    remap = {}
    for new_i, b in enumerate(out):
        remap[id(b)] = new_i
    old_to_new = np.full(len(branches) + 2, -1, np.int64)
    for old_i, b in enumerate(branches):
        old_to_new[old_i] = remap.get(id(b), -1)
    pb = pix_branch
    sel = pb >= 0
    pb[sel] = old_to_new[pb[sel]]
    tmp = pb <= -10
    pb[tmp] = -pb[tmp] - 10
    return out, pb


def _trace(sk: np.ndarray):
    """Split a skeleton into branches between endpoints/junctions.

    Returns (branches, pix_branch) where pix_branch maps every skeleton pixel to the
    index of the branch it belongs to (-2 for junction pixels).
    """
    H, W = sk.shape
    nc = skel.neighbor_count(sk)
    is_node = sk & (nc != 2)
    pix_branch = np.full((H, W), -1, np.int32)
    pix_branch[is_node] = -2
    branches = []
    endpoint = sk & (nc <= 1)
    ys, xs = np.nonzero(sk)
    n = len(ys)
    if n == 0:
        return branches, pix_branch
    # The walk runs over plain Python lists of skeleton pixels and their neighbours
    # (numpy element access is far slower per pixel).
    cid = np.full((H + 2, W + 2), -1, np.int64)
    cid[ys + 1, xs + 1] = np.arange(n)
    nbl = np.stack([cid[ys + 1 + dy, xs + 1 + dx] for dy, dx in _OFFSETS], axis=1).tolist()
    node = is_node[ys, xs].tolist()
    endp = endpoint[ys, xs].tolist()
    _, cluster = cv2.connectedComponents(is_node.astype(np.uint8), connectivity=8)
    clus = cluster[ys, xs].tolist()
    owner = [-2 if v else -1 for v in node]
    xl, yl = xs.tolist(), ys.tolist()
    pref = _PREF  # 4-neighbours first, so the walk does not cut corners

    for p0 in range(n):
        if not node[p0]:
            continue
        c0 = clus[p0]
        for p1 in nbl[p0]:
            if p1 < 0 or node[p1] or owner[p1] != -1:
                continue
            idx = len(branches)
            path = [p0, p1]
            owner[p1] = idx
            prev, cur = p0, p1
            end_node = -1
            while True:
                nb = nbl[cur]
                nxt = -1
                for k in pref:
                    q = nb[k]
                    if q >= 0 and node[q] and q != prev and not (clus[q] == c0 and len(path) <= 2):
                        end_node = q
                        break
                if end_node >= 0:
                    break
                for k in pref:
                    q = nb[k]
                    if q >= 0 and not node[q] and owner[q] == -1:
                        nxt = q
                        break
                if nxt < 0:
                    break
                owner[nxt] = idx
                path.append(nxt)
                prev, cur = cur, nxt
            if end_node >= 0:
                path.append(end_node)
            pts = np.array([(xl[i], yl[i]) for i in path], np.float64)
            b_free = True if end_node < 0 else endp[end_node]
            branches.append(Branch(pts, endp[p0], b_free))
    # Direct node-to-node links (adjacent junction pixels of different clusters) need no
    # branch; endpoints touching a junction become zero-length branches.
    ncl = nc[ys, xs].tolist()
    for p0 in range(n):
        if not (endp[p0] and ncl[p0] == 1):
            continue
        for p1 in nbl[p0]:
            if p1 >= 0 and node[p1] and not endp[p1]:
                branches.append(Branch(np.array([(xl[p0], yl[p0]), (xl[p1], yl[p1])], np.float64), True, False))
                owner[p0] = len(branches) - 1
    pix_branch[ys, xs] = owner
    # Isolated rings (no nodes at all).
    rest = sk & (pix_branch == -1)
    if rest.any():
        n, lab = cv2.connectedComponents(rest.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            ys, xs = np.nonzero(lab == i)
            if len(xs) < 3:
                continue
            pts = np.stack([xs, ys], axis=1).astype(np.float64)
            c = pts.mean(axis=0)
            order = np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))
            ring = pts[order]
            branches.append(Branch(np.vstack([ring, ring[:1]]), False, False))
            pix_branch[ys, xs] = len(branches) - 1
    # Endpoint pixels that start a branch belong to it.
    for i, b in enumerate(branches):
        if b.a_free:
            x, y = int(b.pts[0, 0]), int(b.pts[0, 1])
            pix_branch[y, x] = i
        if b.b_free:
            x, y = int(b.pts[-1, 0]), int(b.pts[-1, 1])
            if pix_branch[y, x] < 0:
                pix_branch[y, x] = i
    return branches, pix_branch


def end_direction(b: Branch, at_a: bool, span: float) -> tuple:
    """Unit direction pointing out of the branch at the given free end, and the end point."""
    pts = b.pts if at_a else b.pts[::-1]
    tip = pts[0]
    # The axis of the straight run behind the tip, skipping the last few pixels: a line
    # cut off at a slant (or by an erased door leaf) curls its skeleton at the very end.
    skip = int(np.ceil(max(2.0, b.width)))
    run = pts[skip:skip + int(2 * span)].astype(np.float64)
    if len(run) >= max(5, span):
        m = run.mean(axis=0)
        evals, evecs = np.linalg.eigh(np.cov((run - m).T))
        if evals[1] > 0 and evals[0] <= 0.02 * evals[1]:
            u = evecs[:, 1]
            if u @ (tip - m) < 0:
                u = -u
            return u, tip
    # Otherwise the mean direction of the points within ``span`` pixels of the tip.
    d = np.hypot(*(pts - tip).T)
    sel = pts[(d > 0) & (d <= span)]
    if len(sel) == 0:
        sel = pts[1:2]
    if len(sel) == 0:
        return None, tip
    v = tip - sel.mean(axis=0)
    n = float(np.hypot(*v))
    if n < 1e-6:
        return None, tip
    return v / n, tip
