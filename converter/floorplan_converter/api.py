"""The conversion pipeline: image in, rooms (and building outline) out."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict

import cv2
import numpy as np
from scipy import ndimage as ndi

from . import __version__
from . import classify, fills, floor, gaps, geometry, graph, layout, rooms, style, text, walls
from .loading import load_image, make_working
from .options import ConverterOptions
from .result import ConversionResult, Door, Room, TextLabel, Wall

log = logging.getLogger(__name__)

DOOR_WIDTH_M = 0.9  # a typical single door, used only for the scale hint


def convert(image, options: ConverterOptions = None, **kwargs) -> ConversionResult:
    """Convert a floorplan image into room polygons.

    ``image`` may be encoded bytes (PNG, JPEG, TIFF, WebP, BMP, PDF with PyMuPDF), a
    file path, a numpy array (BGR, BGRA or gray) or a PIL image. Keyword arguments
    override fields of :class:`ConverterOptions` (e.g. ``debug=True``, ``ocr="none"``).
    """
    opts = options or ConverterOptions()
    if kwargs:
        opts = ConverterOptions.from_kwargs(**{**asdict(opts), **kwargs})
    img = load_image(image, opts.pdf_page, opts.pdf_dpi)
    res, items = _convert(img, opts, top=True, page_text=_pdf_text(image, img, opts.pdf_page))
    if opts.read_floor and "sheets" not in res.meta:  # several floors: each read its own
        t = time.perf_counter()
        res.meta["floor"] = _floor(image, img, items, res, opts)
        timings = res.meta["timings_s"]
        timings["floor"] = round(time.perf_counter() - t, 3)
        timings["total"] = round(timings.get("total", 0.0) + timings["floor"], 3)
    return res


def detect_sheets(image, ocr: str = "auto", pdf_page: int = 0, pdf_dpi: int = 200):
    """Where the floors are on a sheet that shows several, without converting them.

    Returns ``None`` when the image shows one floor (wings of one building count as
    one), else one entry per floor in reading order, like ``meta["sheets"]`` from
    :func:`convert` but with ``outline`` None: ``{"bbox": [x0, y0, x1, y1]`` (image
    pixels), ``"level"``, ``"label"``, ``"confidence"`` (that it is a floor of its
    own), ``"outline"``, ``"floor"`` (as from :func:`read_floor`)``}``.
    """
    opts = ConverterOptions(ocr=ocr, pdf_page=pdf_page, pdf_dpi=pdf_dpi)
    img = load_image(image, opts.pdf_page, opts.pdf_dpi)
    wk = make_working(img, opts.max_working_size, opts.min_working_size)
    sty = style.analyze(wk.bgr)
    page_text = _pdf_text(image, img, opts.pdf_page)
    plans = layout.find((255 - wk.bgr) if sty.inverted else wk.bgr, sty.ink, text.get_backend(opts.ocr),
                        page_text=_scaled_items(page_text, wk.scale))[0]
    if len(plans) < 2:
        return None
    out = []
    for p in plans:
        fl = p.floor
        out.append({"bbox": [round(v / wk.scale, 1) for v in p.bbox], "level": fl["level"] if fl else None,
                    "label": fl["label"] if fl else None, "confidence": p.confidence, "outline": None,
                    "floor": fl})
    return out


def _pdf_text(image, img: np.ndarray, page: int):
    """A PDF input's own text, in the pixels of its rendered page (None otherwise)."""
    pdf = floor.pdf_bytes(image)
    if pdf is None:
        return None
    return floor.pdf_text_items(pdf, page, size=(img.shape[1], img.shape[0])) or None


def _scaled_items(items, scale: float):
    if not items:
        return None
    return [text.TextItem(np.asarray(it.quad, np.float64) * scale, it.text, it.confidence) for it in items]


def _floor(image, img: np.ndarray, items: list, res: ConversionResult, opts: ConverterOptions):
    """Which floor the plan shows (see floor.py), from the PDF's own text when it has
    some, else from the text read during conversion."""
    pdf = floor.pdf_bytes(image)
    if pdf is not None:
        found = floor.floor_from_texts(floor.pdf_text_items(pdf, opts.pdf_page))
        if found is not None and found["confidence"] >= 0.5:
            return found
    crop = res.meta.get("crop")
    if crop is not None and res.meta.get("ocr_backend", "none") != "none":
        # Only the crop around the plan was read; a title or floor number often sits
        # beside it, so read the rest of the sheet too (big text, small image).
        x0, y0, x1, y1 = crop
        rest = img.copy()
        rest[y0:y1, x0:x1] = 255
        for it in floor.ocr_page(rest, text.get_backend(opts.ocr), max_size=1400):
            cx, cy = it.center
            if not (x0 <= cx < x1 and y0 <= cy < y1):
                items.append(it)
    return floor.floor_from_texts(items)


def _convert(img: np.ndarray, opts: ConverterOptions, top: bool = False, focus=None, page_text=None):
    """Run the pipeline; returns the result and the text read (original pixels).

    At the top level the plans on the sheet are found first: the converter then runs
    on each floor drawn on it, cropped around it when that gains resolution. ``focus``
    (x0, y0, x1, y1, image pixels) is the plan to keep."""
    timings = {}
    t_start = t = time.perf_counter()

    def lap(name):
        nonlocal t
        now = time.perf_counter()
        timings[name] = round(now - t, 3)
        t = now

    wk = make_working(img, opts.max_working_size, opts.min_working_size)
    bgr = wk.bgr
    sty = style.analyze(bgr)
    lap("load_and_style")

    backend = text.get_backend(opts.ocr)
    boxes = None
    if top:
        ocr_img = (255 - bgr) if sty.inverted else bgr
        plans, _, _, boxes = layout.find(ocr_img, sty.ink, backend, page_text=_scaled_items(page_text, wk.scale))
        lap("find_plan")
        foci = [tuple(v / wk.scale for v in p.bbox) for p in plans]
        if len(plans) > 1:
            parts = [_convert_part(img, opts, f, True) for f in foci]
            res, items = _merge(parts, plans, img.shape)
            res.meta["timings_s"] = {**timings, "total": round(time.perf_counter() - t_start, 3)}
            return res, items
        focus = foci[0]
        box = _crop_box(plans[0].bbox, wk.scale, img.shape, opts) if opts.auto_crop else None
        if box is not None:
            # The plan fills only part of the sheet: run again on the plan alone, which
            # gets more working resolution for its thin lines.
            res, items = _convert_part(img, opts, focus, box)
            res.meta["timings_s"] = {**timings, **res.meta["timings_s"],
                                     "total": round(time.perf_counter() - t_start, 3)}
            return res, items

    ocr_img = (255 - bgr) if sty.inverted else bgr
    tr = text.find_text(ocr_img, sty.ink, backend, boxes=boxes)
    lap("text")

    W = walls.extract(sty.ink, sty.contrast, tr.mask, tr.text_height,
                      focus=None if focus is None else tuple(v * wk.scale for v in focus))
    barrier = W.barrier
    prune_len = max(2, int(round(0.5 * W.width)) + 1)
    g = graph.build(barrier, W.coverage, prune_len)
    lap("walls")

    gr = gaps.close_gaps(barrier, g, W.width, tr.text_height, opts.max_gap_doors,
                         ray_min_width=0.8 * W.tau, support=W.mask, door_hint=W.door_guess)
    door = gr.door_width
    g_max = opts.max_gap_doors * door
    lap("doors")

    fp = rooms.footprint_from_walls(W.mask, gr.mask, W.width, 2.2 * door)
    interior = fp & ~W.mask
    lab = fills.lab_image(bgr)
    r_out = max(3, int(round(2 * W.width)))
    k_out = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r_out + 1, 2 * r_out + 1))
    outside = ~cv2.dilate((fp | sty.ink | tr.mask).astype(np.uint8), k_out).astype(bool)
    paper = fills.PaperModel(lab, outside)
    painted = fills.painted_fraction(lab, paper, interior)
    has_fills = painted > 0.35
    if has_fills:
        # In a painted plan the paint shows the floor even where the outer wall is
        # broken up (glazing drawn dashed, wide entrances): add painted areas joined to
        # the building to its footprint.
        grown = fills.painted_regions(lab, paper, fp, min_area=(2.0 * door) ** 2, width=W.width)
        if grown.any():
            fp = ndi.binary_fill_holes(fp | grown)
            interior = fp & ~W.mask
    soft = np.zeros_like(fp)
    if has_fills and opts.use_fill_boundaries:
        r = int(np.ceil(W.width)) + 2
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        ignore = cv2.dilate((sty.ink | tr.mask).astype(np.uint8), k).astype(bool)
        soft = fills.fill_boundaries(bgr, ignore, min_len=0.4 * door) & fp
    full_barrier = barrier | gr.mask | soft
    # Separators that are not real walls: light lines, colour edges (closed doorways are
    # deliberate and do not count). In single-weight drawings every line is suspect.
    weak = ((W.mask & ~W.strong) if W.thick_plan else W.mask.copy()) | soft
    weak &= ~gr.mask

    labels = rooms.label_free_space(fp, full_barrier, max(6, int(W.width ** 2)))
    fp_area = float(fp.sum())
    px_per_m = door / DOOR_WIDTH_M if door > 0 else 0.0
    min_area = 1.2 * px_per_m ** 2 if px_per_m > 0 else opts.min_room_fraction * fp_area
    min_area = float(np.clip(min_area, max((3 * W.width) ** 2, opts.min_room_fraction * fp_area), 0.004 * fp_area + 1))

    labels, stair_ids = rooms.group_stairs(labels, door, W.width)
    labels = rooms.merge_small(labels, min_area, W.width, weak, keep_ids=stair_ids)
    if W.thick_plan:
        enclosed_max = 10.0 * px_per_m ** 2 if px_per_m > 0 else 8 * min_area
        labels = rooms.merge_enclosed(labels, enclosed_max, W.width, weak, keep_ids=stair_ids)
    labels = rooms.close_thin_separators(labels, weak | (W.mask & ~W.strong), W.width)
    labels = rooms.fill_region_holes(labels, full_barrier, max(4 * min_area, 1.0))
    labels, cuts = rooms.split_necks(labels, min_area, door, opts.neck_ratio, g_max)
    labels = rooms.merge_small(labels, min_area, W.width, weak, keep_ids=stair_ids)
    if W.pale.any():
        # Zone lines and other colouring drawn across a room do not split it.
        labels = rooms.merge_pale(labels, full_barrier, W.pale, 1.5 * door, tr.mask, gr.mask, keep_ids=stair_ids)
        near_pale = cv2.dilate(W.pale.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        labels = rooms.close_thin_separators(labels, full_barrier & near_pale, W.width)
    lap("rooms")

    # Unpainted areas inside a painted plan are outdoor (decks, terraces, courtyards).
    ids = [int(v) for v in np.unique(labels) if v > 0]
    outdoor_ids = set()
    if has_fills:
        page = fills.page_like(lab, paper, labels, ids, sty.ink | tr.mask)
        outdoor_ids = {rid for rid, v in page.items() if v}

    angle = geometry.dominant_angle(W.strong) if W.strong.any() else 0.0
    eps = max(0.7, opts.simplify_factor * W.width)
    scale = wk.scale

    def to_orig(pts):
        return np.asarray(pts, np.float64) / scale  # corner convention scales directly

    # Room extent.
    shape_labels = labels
    if opts.room_extent == "centerline":
        from skimage.segmentation import expand_labels

        shape_labels = expand_labels(labels, distance=max(1.0, 1.5 * W.width)) * fp

    # Text assignment.
    by_room = classify.assign_texts(labels, tr.items, max_dist=max(2.0, 1.5 * W.width))
    room_of_text = {}
    reach = int(max(2, np.ceil(W.width) + 2))
    objs = ndi.find_objects(shape_labels)

    out_rooms = []
    outdoor_mask = np.zeros_like(fp)
    lab_objs = ndi.find_objects(labels)
    H, Wd = labels.shape
    for rid in ids:
        sl = objs[rid - 1] if rid - 1 < len(objs) else None
        lsl = lab_objs[rid - 1] if rid - 1 < len(lab_objs) else None
        if sl is None or lsl is None:
            continue
        # Work in a crop around the room (with room for the wall ring).
        y0 = max(0, min(sl[0].start, lsl[0].start) - reach - 1)
        y1 = min(H, max(sl[0].stop, lsl[0].stop) + reach + 1)
        x0 = max(0, min(sl[1].start, lsl[1].start) - reach - 1)
        x1 = min(Wd, max(sl[1].stop, lsl[1].stop) + reach + 1)
        crop = (slice(y0, y1), slice(x0, x1))
        shape_m = shape_labels[crop] == rid
        room_m = labels[crop] == rid
        polys = geometry.mask_polygons(shape_m, eps, min_hole_area=max(4 * W.width ** 2, 0.05 * min_area))
        if not polys:
            continue
        polys.sort(key=lambda p: -geometry.polygon_area(p[0]))
        outer, holes = polys[0]
        off = np.array([x0, y0], np.float64)
        outer, holes = outer + off, [h + off for h in holes]
        outer, holes = geometry.clean_polygon(outer, holes, angle, opts.orthogonalize, eps)

        entries = by_room.get(rid, [])
        full, name, number = classify.label_room(entries)
        kind = classify.kind_from_label(full) if full else None
        if kind is None:
            if rid in stair_ids:
                kind = "stair"
            elif rid in outdoor_ids:
                kind = "outdoor"
            elif classify.has_cross(sty.ink[crop], room_m):
                kind = "elevator"
            else:
                st = classify.shape_stats(room_m)
                n_nb = classify.neighbour_count(labels[crop], rid, (slice(0, y1 - y0), slice(0, x1 - x0)), reach)
                if st["elongation"] >= 6 and st["width"] <= 3.5 * door and n_nb >= 3:
                    kind = "corridor"
                else:
                    kind = "room"
        if kind == "outdoor":
            outdoor_mask[crop] |= room_m

        # Confidence: how much of the room's boundary is real wall rather than a guess.
        k = np.ones((2 * reach + 1, 2 * reach + 1), np.uint8)
        ring = cv2.dilate(room_m.astype(np.uint8), k).astype(bool) & ~room_m
        sep = ring & full_barrier[crop]
        conf = float((sep & W.mask[crop] & ~gr.mask[crop]).sum() / max(1, sep.sum()))

        dt = cv2.distanceTransform(shape_m.astype(np.uint8), cv2.DIST_L2, 3)
        cy, cx = np.unravel_index(int(np.argmax(dt)), dt.shape)
        room_id = f"r{len(out_rooms) + 1}"
        for kk, _ in entries:
            room_of_text[kk] = room_id
        out_rooms.append(Room(
            id=room_id,
            polygon=_pts(to_orig(outer)),
            holes=[_pts(to_orig(h)) for h in holes],
            label=full, name=name, number=number, kind=kind,
            area_px=round(float(shape_m.sum()) / scale ** 2, 1),
            confidence=round(conf, 2),
            centroid=_pt(to_orig(np.array([cx + x0 + 0.5, cy + y0 + 0.5]))),
        ))
    lap("polygons")

    # Building outline (outdoor areas excluded).
    fp_out = fp & ~outdoor_mask
    if outdoor_mask.any():
        r = max(1, int(round(W.width)))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        # Drop the thin walls that only bounded the outdoor areas.
        fp_out = cv2.morphologyEx(fp_out.astype(np.uint8), cv2.MORPH_OPEN, k).astype(bool)
        fp_out = rooms._main_components(fp_out)
    fp_polys = geometry.mask_polygons(fp_out, max(eps, 1.0), min_hole_area=min_area)
    footprint = []
    for outer, holes in sorted(fp_polys, key=lambda p: -geometry.polygon_area(p[0])):
        outer, holes = geometry.clean_polygon(outer, holes, angle, opts.orthogonalize, eps)
        footprint.append({"polygon": _pts(to_orig(outer)), "holes": [_pts(to_orig(h)) for h in holes]})
    outline = footprint[0]["polygon"] if footprint else None

    # Doors and openings.
    out_doors = [Door(_pts(to_orig(np.array([gp.p, gp.q]) + 0.5)), round(gp.length / scale, 1), "door") for gp in gr.gaps]
    for p, q in cuts:
        out_doors.append(Door(_pts(to_orig(np.array([p, q]) + 0.5)), round(float(np.hypot(*(q - p))) / scale, 1), "opening"))

    # Wall centre lines.
    out_walls = []
    for b in g.branches:
        if b.length < max(3.0, 1.5 * W.width):
            continue
        pts = cv2.approxPolyDP(b.pts.astype(np.float32).reshape(-1, 1, 2), max(0.7, 0.5 * W.width), False).reshape(-1, 2)
        out_walls.append(Wall(_pts(to_orig(pts + 0.5)), round(b.width / scale, 2)))

    texts_out = [TextLabel(it.text, _pts(to_orig(it.quad + 0.5)), round(it.confidence, 3), room_of_text.get(k))
                 for k, it in enumerate(tr.items) if it.text and it.confidence >= 0.5]

    scale_hint = round(door / scale / DOOR_WIDTH_M, 2) if len([x for x in gr.gaps if x.kind == "pair"]) >= 3 else None
    lap("outputs")

    result = ConversionResult(
        image_size=[int(img.shape[1]), int(img.shape[0])],
        outline=outline,
        rooms=out_rooms,
        footprint=footprint,
        walls=out_walls,
        doors=out_doors,
        texts=texts_out,
        scale_hint=scale_hint,
        meta={
            "converter_version": __version__,
            "working_scale": round(scale, 5),
            "ocr_backend": backend.name,
            "style": {
                "inverted": sty.inverted,
                "double_line_walls": W.double_line,
                "heavy_walls": W.thick_plan,
                "filled_rooms": has_fills,
                "colored_ink": sty.colored_ink,
            },
            "wall_thickness_px": round(W.width / scale, 2),
            "door_width_px": round(door / scale, 2),
            "rotation_deg": round(angle, 2),
            "timings_s": timings,
        },
    )
    if opts.debug:
        from . import debug

        result.debug_images = debug.render(bgr, sty, tr, W, gr, soft, labels, out_rooms, scale)
        timings["debug"] = round(time.perf_counter() - t, 3)
    timings["total"] = round(time.perf_counter() - t_start, 3)
    return result, [text.TextItem(it.quad / scale, it.text, it.confidence) for it in tr.items]


def _convert_part(img: np.ndarray, opts: ConverterOptions, focus, box):
    """Convert the plan at ``focus`` (image pixels) on a crop around it. ``box`` is the
    crop, or True for the usual margin around the plan."""
    if box is True:
        x0, y0, x1, y1 = focus
        m = 0.12 * max(x1 - x0, y1 - y0) + 16
        h, w = img.shape[:2]
        box = (max(0, int(x0 - m)), max(0, int(y0 - m)), min(w, int(np.ceil(x1 + m))), min(h, int(np.ceil(y1 + m))))
    x0, y0, x1, y1 = box
    f = tuple(float(v) for v in np.subtract(focus, (x0, y0, x0, y0)))
    res, items = _convert(img[y0:y1, x0:x1], opts, focus=f)
    res = _translate(res, x0, y0, img.shape)
    for it in items:
        it.quad = it.quad + np.array([x0, y0], np.float64)
    res.meta["crop"] = [x0, y0, x1, y1]
    return res, items


def _merge(parts: list, plans: list, shape):
    """One result for a sheet of several floors: everything in sheet pixels, and
    ``meta["sheets"]`` saying where each floor is and which it is."""
    rooms, texts, walls_, doors, footprint, sheets, items = [], [], [], [], [], [], []
    for (res, its), plan in zip(parts, plans):
        ids = {}
        for r in res.rooms:
            ids[r.id] = r.id = f"r{len(rooms) + 1}"
            rooms.append(r)
        for t in res.texts:
            t.room_id = ids.get(t.room_id)
        texts += res.texts
        walls_ += res.walls
        doors += res.doors
        footprint += res.footprint
        items += its
        own = floor.floor_from_texts(its)
        fl = plan.floor if plan.floor is not None and (own is None or plan.floor["confidence"] >= own["confidence"]) else own
        res.meta["floor"] = fl
        pts = np.array([p for part in res.footprint for p in part["polygon"]] or [[0.0, 0.0]])
        bbox = ([round(float(v), 1) for v in (*pts.min(axis=0), *pts.max(axis=0))] if res.footprint
                else [round(float(v), 1) for v in res.meta["crop"]])
        sheets.append({"bbox": bbox, "level": fl["level"] if fl else None, "label": fl["label"] if fl else None,
                       "confidence": plan.confidence, "outline": res.outline, "floor": fl})
    areas = [_ring_area(res.outline) for res, _ in parts]
    big = parts[int(np.argmax(areas))][0]
    hints = [res.scale_hint for res, _ in parts if res.scale_hint]
    meta = {k: v for k, v in big.meta.items() if k != "crop"}
    meta["sheets"] = sheets
    out = ConversionResult(
        image_size=[int(shape[1]), int(shape[0])],
        outline=big.outline, rooms=rooms, footprint=footprint, walls=walls_, doors=doors, texts=texts,
        scale_hint=round(float(np.median(hints)), 2) if hints else None, meta=meta,
    )
    out.debug_images = big.debug_images
    return out, items


def _ring_area(ring) -> float:
    if not ring:
        return 0.0
    p = np.asarray(ring, np.float64)
    return 0.5 * abs(float(np.dot(p[:, 0], np.roll(p[:, 1], 1)) - np.dot(p[:, 1], np.roll(p[:, 0], 1))))


def _crop_box(bbox, scale: float, shape, opts: ConverterOptions):
    """Crop (original pixels) around the plan when the sheet around it is mostly empty
    and the crop would be worked at a usefully higher resolution, else None."""
    h, w = shape[:2]
    x0, y0, x1, y1 = (v / scale for v in bbox)
    side = max(x1 - x0, y1 - y0)
    if side <= 0:
        return None
    # A generous margin: the paper around the plan is what the ink and fill colours
    # are measured against.
    m = 0.12 * side + 16
    x0, y0 = max(0, int(x0 - m)), max(0, int(y0 - m))
    x1, y1 = min(w, int(np.ceil(x1 + m))), min(h, int(np.ceil(y1 + m)))
    longest = max(x1 - x0, y1 - y0)
    if longest >= 0.8 * max(h, w) or (x1 - x0) * (y1 - y0) > 0.35 * h * w:
        return None
    if longest > opts.max_working_size:
        new_scale = opts.max_working_size / longest
    elif longest < opts.min_working_size:
        new_scale = min(opts.min_working_size / longest, 3.0)
    else:
        new_scale = 1.0
    if new_scale < 1.2 * scale:
        return None
    return x0, y0, x1, y1


def _translate(res: ConversionResult, dx: float, dy: float, shape) -> ConversionResult:
    """Shift every coordinate of a result computed on a crop back to the full image."""
    d = np.array([dx, dy], np.float64)

    def ring(pts):
        return _pts(np.asarray(pts, np.float64).reshape(-1, 2) + d) if pts else pts

    for r in res.rooms:
        r.polygon = ring(r.polygon)
        r.holes = [ring(hh) for hh in r.holes]
        r.centroid = ring([r.centroid])[0]
    res.footprint = [{"polygon": ring(f["polygon"]), "holes": [ring(hh) for hh in f["holes"]]} for f in res.footprint]
    res.outline = ring(res.outline) if res.outline else res.outline
    for x in res.walls:
        x.polyline = ring(x.polyline)
    for x in res.doors:
        x.polyline = ring(x.polyline)
    for x in res.texts:
        x.quad = ring(x.quad)
    res.image_size = [int(shape[1]), int(shape[0])]
    return res


def _pts(a):
    return [[round(float(x), 2), round(float(y), 2)] for x, y in np.asarray(a).reshape(-1, 2)]


def _pt(a):
    a = np.asarray(a, np.float64).reshape(-1)
    return [round(float(a[0]), 2), round(float(a[1]), 2)]

