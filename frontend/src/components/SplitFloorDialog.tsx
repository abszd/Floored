import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Columns2, Crop, Rows2, Scissors, Sparkles, Trash2 } from "lucide-react";
import clsx from "clsx";
import { api, mediaUrl } from "../lib/api";
import { useJobs } from "../lib/jobs";
import type { Floor } from "../lib/types";
import { Modal, Spinner } from "./ui";
import { useToast } from "./Toast";

type Box = [number, number, number, number];
interface Region {
  bbox: Box;
  level: string;
  name: string;
  /** From the converter's guess rather than drawn by hand. */
  suggested?: boolean;
}

const COLORS = ["#4f46e5", "#0891b2", "#16a34a", "#d97706", "#db2777", "#7c3aed"];

function norm([x0, y0, x1, y1]: Box): Box {
  return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
}

function roomsBox(f: Floor): Box | null {
  const pts = f.rooms_px.flat();
  if (!pts.length) return null;
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function pad(b: Box, w: number, h: number, frac = 0.02): Box {
  const d = frac * Math.max(w, h);
  return [Math.max(0, b[0] - d), Math.max(0, b[1] - d), Math.min(w, b[2] + d), Math.min(h, b[3] + d)];
}

type Drag =
  | { kind: "new"; start: [number, number] }
  | { kind: "move"; i: number; start: [number, number]; orig: Box }
  | { kind: "resize"; i: number; fixed: [number, number] };

/**
 * Crop a plan to the building, or cut a sheet that shows several floors into
 * one floor each. Drag on the plan to mark each floor; the first area stays
 * this floor, the others become new floors of the building. Room numbers and
 * other fixes carry over.
 */
export default function SplitFloorDialog({ floorId, onClose }: { floorId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track } = useJobs();
  const detail = useQuery({ queryKey: ["floor", floorId], queryFn: () => api.floor(floorId) });
  const f = detail.data;
  const [w, h] = f?.image_size ?? [1000, 1000];
  const [regions, setRegions] = useState<Region[]>([]);
  const [refit, setRefit] = useState(true);
  const [reconvert, setReconvert] = useState(false);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [draft, setDraft] = useState<Box | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const loaded = useRef(false);

  // Start from the converter's guess when it thinks the sheet holds several floors.
  useEffect(() => {
    if (!f || loaded.current) return;
    loaded.current = true;
    if (f.sheets.length > 1) {
      setRegions(
        f.sheets.map((s) => ({ bbox: s.bbox, level: s.level != null ? String(s.level) : "", name: s.name ?? "", suggested: true })),
      );
    }
  }, [f]);

  const toImage = (e: { clientX: number; clientY: number }): [number, number] => {
    const el = svg.current!;
    const pt = el.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    const p = pt.matrixTransform(el.getScreenCTM()!.inverse());
    return [Math.max(0, Math.min(w, p.x)), Math.max(0, Math.min(h, p.y))];
  };

  const onDown = (e: React.PointerEvent, d: Drag) => {
    e.stopPropagation();
    e.preventDefault();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setDrag(d);
  };
  const onMove = (e: React.PointerEvent) => {
    if (!drag) return;
    const [x, y] = toImage(e);
    if (drag.kind === "new") setDraft(norm([drag.start[0], drag.start[1], x, y]));
    else if (drag.kind === "resize") setRegions((rs) => rs.map((r, i) => (i === drag.i ? { ...r, bbox: norm([drag.fixed[0], drag.fixed[1], x, y]) } : r)));
    else {
      const [x0, y0, x1, y1] = drag.orig;
      const dx = Math.max(-x0, Math.min(w - x1, x - drag.start[0]));
      const dy = Math.max(-y0, Math.min(h - y1, y - drag.start[1]));
      setRegions((rs) => rs.map((r, i) => (i === drag.i ? { ...r, bbox: [x0 + dx, y0 + dy, x1 + dx, y1 + dy] } : r)));
    }
  };
  const onUp = () => {
    if (drag?.kind === "new" && draft) {
      const big = draft[2] - draft[0] > 0.02 * w && draft[3] - draft[1] > 0.02 * h;
      if (big) setRegions((rs) => [...rs, { bbox: draft, level: "", name: "" }]);
    }
    setDraft(null);
    setDrag(null);
  };

  const rb = f ? roomsBox(f) : null;
  const presets = {
    rooms: () => rb && setRegions([{ bbox: pad(rb, w, h), level: "", name: "" }]),
    rows: () =>
      setRegions([
        { bbox: [0, 0, w, h / 2], level: "", name: "" },
        { bbox: [0, h / 2, w, h], level: "", name: "" },
      ]),
    cols: () =>
      setRegions([
        { bbox: [0, 0, w / 2, h], level: "", name: "" },
        { bbox: [w / 2, 0, w, h], level: "", name: "" },
      ]),
  };

  const bad = regions.some((r) => r.level.trim() !== "" && Number.isNaN(Number(r.level)));
  const split = useMutation({
    mutationFn: () =>
      api.splitFloor(floorId, {
        regions: regions.map((r) => ({
          bbox: r.bbox.map(Math.round) as Box,
          level: r.level.trim() === "" ? null : Number(r.level),
          name: r.name.trim() || null,
        })),
        refit,
        reconvert,
      }),
    onSuccess: async ({ floors, job_ids }) => {
      job_ids.forEach((id) => track(id, "Finding rooms", { quiet: true }));
      await qc.invalidateQueries();
      toast.success(
        floors.length > 1 ? `Split into ${floors.length} floors` : "Plan cropped",
        job_ids.length ? "Finding rooms in the parts that had none." : "Undo it from the floor's menu if it's not right.",
      );
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't split the plan", e.message),
  });

  // Dim everything outside the marked areas, so it's clear what gets cut away.
  const shade = useMemo(() => {
    const boxes = [...regions.map((r) => r.bbox), ...(draft ? [draft] : [])];
    if (!boxes.length) return null;
    return `M0 0H${w}V${h}H0Z ` + boxes.map(([x0, y0, x1, y1]) => `M${x0} ${y0}H${x1}V${y1}H${x0}Z`).join(" ");
  }, [regions, draft, w, h]);
  const stroke = Math.max(w, h) / 400;
  const handle = Math.max(w, h) / 70;
  const font = Math.max(w, h) / 45;

  return (
    <Modal title={f ? `Crop or split ${f.name}` : "Crop or split"} onClose={onClose} wide="xl">
      {!f ? (
        <Spinner />
      ) : (
        <div className="flex flex-col gap-4 md:flex-row">
          <div className="min-w-0 flex-1">
            <div className="mb-2 flex flex-wrap items-center gap-1.5 text-xs">
              <span className="mr-1 text-slate-500">Quick start:</span>
              <button className="btn-secondary px-2 py-1 text-xs" disabled={!rb} onClick={presets.rooms} title="Crop to the rooms the converter found">
                <Crop className="h-3.5 w-3.5" /> Crop to the rooms
              </button>
              <button className="btn-secondary px-2 py-1 text-xs" onClick={presets.rows}>
                <Rows2 className="h-3.5 w-3.5" /> Top and bottom
              </button>
              <button className="btn-secondary px-2 py-1 text-xs" onClick={presets.cols}>
                <Columns2 className="h-3.5 w-3.5" /> Left and right
              </button>
              {regions.length > 0 && (
                <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setRegions([])}>
                  Clear
                </button>
              )}
            </div>
            <div className="rounded-lg border border-slate-200 bg-slate-50">
              <svg
                ref={svg}
                viewBox={`0 0 ${w} ${h}`}
                className="block max-h-[62vh] w-full touch-none select-none"
                style={{ cursor: drag?.kind === "move" ? "grabbing" : "crosshair" }}
                onPointerDown={(e) => onDown(e, { kind: "new", start: toImage(e) })}
                onPointerMove={onMove}
                onPointerUp={onUp}
                onPointerCancel={onUp}
                data-testid="split-canvas"
              >
                <image href={mediaUrl(f.image_url)} width={w} height={h} />
                {f.rooms_px.map((ring, i) => (
                  <polygon key={i} points={ring.map((p) => p.join(",")).join(" ")} fill="#6366f1" fillOpacity={0.12} stroke="#6366f1" strokeOpacity={0.4} strokeWidth={stroke / 2} />
                ))}
                {f.outline_px && (
                  <polygon points={f.outline_px.map((p) => p.join(",")).join(" ")} fill="none" stroke="#f43f5e" strokeWidth={stroke} strokeDasharray={`${stroke * 4} ${stroke * 3}`} />
                )}
                {shade && <path d={shade} fill="#0f172a" fillOpacity={0.35} fillRule="evenodd" pointerEvents="none" />}
                {regions.map((r, i) => {
                  const [x0, y0, x1, y1] = r.bbox;
                  const c = COLORS[i % COLORS.length];
                  return (
                    <g key={i}>
                      <rect
                        x={x0}
                        y={y0}
                        width={x1 - x0}
                        height={y1 - y0}
                        fill={c}
                        fillOpacity={0.05}
                        stroke={c}
                        strokeWidth={stroke * 1.5}
                        strokeDasharray={r.suggested ? `${stroke * 5} ${stroke * 3}` : undefined}
                        style={{ cursor: "grab" }}
                        onPointerDown={(e) => onDown(e, { kind: "move", i, start: toImage(e), orig: r.bbox })}
                      />
                      <text x={x0 + font * 0.4} y={y0 + font * 1.1} fontSize={font} fontWeight={700} fill={c} pointerEvents="none">
                        {i + 1}
                      </text>
                      {(
                        [
                          [x0, y0, x1, y1],
                          [x1, y0, x0, y1],
                          [x1, y1, x0, y0],
                          [x0, y1, x1, y0],
                        ] as const
                      ).map(([hx, hy, fx, fy], k) => (
                        <rect
                          key={k}
                          x={hx - handle / 2}
                          y={hy - handle / 2}
                          width={handle}
                          height={handle}
                          fill="white"
                          stroke={c}
                          strokeWidth={stroke}
                          style={{ cursor: k % 2 ? "nesw-resize" : "nwse-resize" }}
                          onPointerDown={(e) => onDown(e, { kind: "resize", i, fixed: [fx, fy] })}
                        />
                      ))}
                    </g>
                  );
                })}
                {draft && <rect x={draft[0]} y={draft[1]} width={draft[2] - draft[0]} height={draft[3] - draft[1]} fill="none" stroke="#4f46e5" strokeWidth={stroke * 1.5} pointerEvents="none" />}
              </svg>
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
              <span>Drag on the plan to mark an area. Drag an area to move it, or its corners to resize.</span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block h-0 w-4 border-t-2 border-dashed border-rose-500" /> what the converter took as the building
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-indigo-500/30" /> rooms found
              </span>
            </div>
          </div>

          <div className="flex w-full shrink-0 flex-col md:w-72">
            {f.sheets.length > 1 && (
              <div className="mb-3 flex gap-2 rounded-lg bg-indigo-50 px-3 py-2 text-xs text-indigo-800">
                <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                The converter thinks this sheet shows {f.sheets.length} floors. Check the dashed areas, then split.
              </div>
            )}
            {regions.length === 0 ? (
              <div className="rounded-lg border border-dashed border-slate-200 p-4 text-sm text-slate-500">
                <p>Mark one area to crop the plan to it, for example to cut off a title block the converter took for the building.</p>
                <p className="mt-2">Mark one area per floor to split a sheet that shows several floors.</p>
              </div>
            ) : (
              <div className="space-y-2">
                {regions.map((r, i) => (
                  <div key={i} className="rounded-lg border border-slate-200 p-2.5">
                    <div className="mb-1.5 flex items-center gap-2 text-xs font-medium">
                      <span className="flex h-5 w-5 items-center justify-center rounded text-[11px] font-bold text-white" style={{ background: COLORS[i % COLORS.length] }}>
                        {i + 1}
                      </span>
                      <span className="flex-1">{i === 0 ? `Stays ${f.name}` : "New floor"}</span>
                      <button className="p-0.5 text-slate-300 hover:text-rose-500" onClick={() => setRegions((rs) => rs.filter((_, j) => j !== i))} aria-label="Remove area">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    <div className="flex gap-1.5">
                      <input
                        className={clsx("input w-16 px-2 py-1 text-xs", r.level.trim() && Number.isNaN(Number(r.level)) && "border-rose-300")}
                        placeholder={i === 0 ? String(f.level) : "Level"}
                        title="0 is the ground floor, 1 the floor above, -1 the basement"
                        value={r.level}
                        onChange={(e) => setRegions((rs) => rs.map((x, j) => (j === i ? { ...x, level: e.target.value } : x)))}
                      />
                      <input
                        className="input flex-1 px-2 py-1 text-xs"
                        placeholder={i === 0 ? f.name : "Name (optional)"}
                        value={r.name}
                        onChange={(e) => setRegions((rs) => rs.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                      />
                    </div>
                  </div>
                ))}
                <p className="text-[11px] text-slate-500">Leave a level empty to {regions.length > 1 ? "put new floors on the next free level" : "keep it"}.</p>
              </div>
            )}

            <div className="mt-4 space-y-2 text-sm">
              <label className="flex items-start gap-2">
                <input type="checkbox" className="mt-1 accent-indigo-600" checked={refit} onChange={(e) => setRefit(e.target.checked)} />
                <span>
                  Fit each part to the building outline
                  <span className="block text-xs text-slate-500">Off keeps the plan exactly where it is on the map.</span>
                </span>
              </label>
              <label className="flex items-start gap-2">
                <input type="checkbox" className="mt-1 accent-indigo-600" checked={reconvert} onChange={(e) => setReconvert(e.target.checked)} />
                <span>
                  Find rooms again in each part
                  <span className="block text-xs text-slate-500">Replaces room numbers you fixed. Parts with no rooms always are.</span>
                </span>
              </label>
            </div>

            <div className="mt-auto flex justify-end gap-2 pt-4">
              <button className="btn-ghost" onClick={onClose}>
                Cancel
              </button>
              <button className="btn-primary" disabled={!regions.length || bad || split.isPending} onClick={() => split.mutate()}>
                {split.isPending ? <Spinner /> : regions.length > 1 ? <Scissors className="h-4 w-4" /> : <Crop className="h-4 w-4" />}
                {regions.length > 1 ? `Split into ${regions.length} floors` : "Crop"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}
