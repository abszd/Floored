"""Skeleton helpers: thinning, spur pruning, endpoints and tracing into polylines."""

from __future__ import annotations

import cv2
import numpy as np
from skimage.morphology import skeletonize as _sk

_K8 = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.float32)
_OFFSETS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def skeletonize(mask: np.ndarray) -> np.ndarray:
    return _sk(mask.astype(bool))


def neighbor_count(sk: np.ndarray) -> np.ndarray:
    return cv2.filter2D(sk.astype(np.uint8), cv2.CV_16S, _K8, borderType=cv2.BORDER_CONSTANT) * sk


def endpoints(sk: np.ndarray) -> np.ndarray:
    return sk & (neighbor_count(sk) == 1)


def prune(sk: np.ndarray, length: int) -> np.ndarray:
    """Remove side branches shorter than ``length`` pixels, keeping main branches whole."""
    if length <= 0 or not sk.any():
        return sk
    pruned = sk.copy()
    for _ in range(length):
        ends = endpoints(pruned)
        if not ends.any():
            break
        pruned &= ~ends
    # Regrow the surviving branches' tips along the original skeleton.
    grow = endpoints(pruned)
    k = np.ones((3, 3), np.uint8)
    acc = grow.copy()
    for _ in range(length):
        nxt = cv2.dilate(grow.astype(np.uint8), k).astype(bool) & sk & ~pruned & ~acc
        if not nxt.any():
            break
        acc |= nxt
        grow = nxt
    out = pruned | acc
    # Isolated tiny skeletons (dots, short ticks) vanish entirely during pruning; that is intended.
    return out


def trace_back(sk: np.ndarray, start: tuple, max_len: int) -> list:
    """Walk from an endpoint along the skeleton, stopping at junctions or after max_len pixels."""
    h, w = sk.shape
    path = [start]
    prev = None
    cur = start
    for _ in range(max_len):
        y, x = cur
        nbrs = []
        for dy, dx in _OFFSETS:
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and sk[ny, nx] and (ny, nx) != prev and (ny, nx) not in path[-3:]:
                nbrs.append((ny, nx))
        if len(nbrs) != 1:
            # Junction (or end): allow stepping into a junction once but stop after.
            if not nbrs:
                break
            # Prefer 4-neighbours to avoid diagonal shortcuts around corners.
            nbrs.sort(key=lambda p: abs(p[0] - y) + abs(p[1] - x))
            if len(nbrs) > 2:
                break
        prev, cur = cur, nbrs[0]
        path.append(cur)
    return path


def trace_polylines(sk: np.ndarray, min_len: int = 3) -> list:
    """Decompose a skeleton into polylines running between endpoints and junctions.

    Returns a list of (N, 2) arrays of (x, y) pixel coordinates.
    """
    sk = sk.astype(bool).copy()
    h, w = sk.shape
    nc = neighbor_count(sk)
    nodes = sk & (nc != 2)
    visited_edges = set()
    lines = []

    def nbrs(p):
        y, x = p
        out = []
        for dy, dx in _OFFSETS:
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and sk[ny, nx]:
                out.append((ny, nx))
        return out

    node_list = list(zip(*np.nonzero(nodes)))
    for n in node_list:
        for nb in nbrs(n):
            key = (n, nb) if n < nb else (nb, n)
            if key in visited_edges:
                continue
            path = [n, nb]
            visited_edges.add(key)
            prev, cur = n, nb
            while not nodes[cur]:
                nxt = [q for q in nbrs(cur) if q != prev and q not in path[-3:]]
                if not nxt:
                    break
                nxt.sort(key=lambda q: abs(q[0] - cur[0]) + abs(q[1] - cur[1]))
                q = nxt[0]
                k2 = (cur, q) if cur < q else (q, cur)
                if k2 in visited_edges:
                    break
                visited_edges.add(k2)
                prev, cur = cur, q
                path.append(cur)
            if len(path) >= min_len:
                lines.append(np.array([(x, y) for y, x in path], np.float64))
    # Closed loops with no nodes (e.g. a ring) are traced from an arbitrary pixel.
    remaining = sk.copy()
    for line in lines:
        remaining[line[:, 1].astype(int), line[:, 0].astype(int)] = False
    if remaining.any():
        n, lab = cv2.connectedComponents(remaining.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            ys, xs = np.nonzero(lab == i)
            if len(xs) < max(min_len, 8):
                continue
            pts = np.stack([xs, ys], axis=1).astype(np.float32)
            # Order ring pixels by angle around their centroid.
            c = pts.mean(axis=0)
            order = np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))
            ring = pts[order].astype(np.float64)
            lines.append(np.vstack([ring, ring[:1]]))
    return lines
