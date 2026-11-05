import { useEffect, useRef } from "react";
import type { GeoJSONSource, Map as MBMap, MapMouseEvent } from "mapbox-gl";
import type { Feature } from "geojson";
import { BoxSelect, Crop, PenLine, Scissors, Undo2, X } from "lucide-react";
import clsx from "clsx";

export type Tool = "select" | "draw" | "split";
type LngLat = [number, number];

const SRC = "draw-temp";

/** Shows the line or shape being drawn, with a rubber band to the cursor. */
export function useDrawLayer(map: MBMap | null, tool: Tool | null, points: LngLat[]) {
  const pts = useRef(points);
  pts.current = points;
  const cursor = useRef<LngLat | null>(null);

  useEffect(() => {
    if (!map || (tool !== "draw" && tool !== "split")) return;
    const render = () => {
      const p = cursor.current ? [...pts.current, cursor.current] : pts.current;
      const features: Feature[] = pts.current.map((c) => ({ type: "Feature", geometry: { type: "Point", coordinates: c }, properties: {} }));
      if (p.length >= 2) {
        const ring = tool === "draw" && p.length >= 3 ? [...p, p[0]] : p;
        features.push({ type: "Feature", geometry: { type: "LineString", coordinates: ring }, properties: {} });
      }
      if (tool === "draw" && p.length >= 3) {
        features.push({ type: "Feature", geometry: { type: "Polygon", coordinates: [[...p, p[0]]] }, properties: {} });
      }
      (map.getSource(SRC) as GeoJSONSource | undefined)?.setData({ type: "FeatureCollection", features });
    };
    const add = () => {
      if (map.getSource(SRC)) return;
      map.addSource(SRC, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      const slot = map.getStyle()?.imports?.length ? { slot: "top" as const } : {};
      map.addLayer({ id: `${SRC}-fill`, type: "fill", source: SRC, ...slot, filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": "#4f46e5", "fill-opacity": 0.2, "fill-emissive-strength": 1 } });
      map.addLayer({ id: `${SRC}-line`, type: "line", source: SRC, ...slot, filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": tool === "split" ? "#e11d48" : "#4f46e5", "line-width": 2.5, "line-dasharray": [2, 1], "line-emissive-strength": 1 } });
      map.addLayer({ id: `${SRC}-pts`, type: "circle", source: SRC, ...slot, filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 4.5, "circle-color": "#fff", "circle-stroke-color": "#4f46e5", "circle-stroke-width": 2, "circle-emissive-strength": 1 } });
    };
    add();
    render();
    const onMove = (e: MapMouseEvent) => {
      cursor.current = [e.lngLat.lng, e.lngLat.lat];
      render();
    };
    map.on("mousemove", onMove);
    map.doubleClickZoom.disable();
    const timer = setInterval(render, 250); // picks up new points without re-adding listeners
    return () => {
      clearInterval(timer);
      map.off("mousemove", onMove);
      map.doubleClickZoom.enable();
      cursor.current = null;
      for (const id of [`${SRC}-fill`, `${SRC}-line`, `${SRC}-pts`]) if (map.getLayer(id)) map.removeLayer(id);
      if (map.getSource(SRC)) map.removeSource(SRC);
    };
  }, [map, tool]);
}

/** Tools for fixing the plan shown on the map. */
export function FloorToolbar({
  tool,
  onTool,
  canSplit,
  undoLabel,
  onUndo,
  onCrop,
}: {
  tool: Tool | null;
  onTool: (t: Tool | null) => void;
  canSplit: boolean;
  undoLabel: string | null;
  onUndo: () => void;
  onCrop: () => void;
}) {
  const btn = (t: Tool, icon: React.ReactNode, label: string, title: string, disabled = false) => (
    <button
      className={clsx("flex items-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs font-medium transition disabled:opacity-40", tool === t ? "bg-indigo-600 text-white" : "text-slate-600 hover:bg-slate-100")}
      disabled={disabled}
      title={title}
      onClick={() => onTool(tool === t ? null : t)}
    >
      {icon}
      {label}
    </button>
  );
  return (
    <div className="panel flex items-center gap-0.5 p-1" data-testid="floor-tools">
      {btn("select", <BoxSelect className="h-3.5 w-3.5" />, "Select area", "Drag a box to pick every room, wall and door inside it (or hold shift and drag)")}
      {btn("draw", <PenLine className="h-3.5 w-3.5" />, "Draw room", "Click the corners of a room the converter missed")}
      {btn("split", <Scissors className="h-3.5 w-3.5" />, "Split room", canSplit ? "Click two points to cut the selected room in two" : "Select one room first", !canSplit && tool !== "split")}
      <span className="mx-1 h-5 w-px bg-slate-200" />
      <button className="flex items-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100" title="Crop off a title block, or split a sheet with several floors" onClick={onCrop}>
        <Crop className="h-3.5 w-3.5" /> Crop or split
      </button>
      <button
        className="flex items-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 disabled:opacity-40"
        disabled={!undoLabel}
        title={undoLabel ? `Undo ${undoLabel.toLowerCase()} (Ctrl+Z)` : "Nothing to undo"}
        onClick={onUndo}
      >
        <Undo2 className="h-3.5 w-3.5" /> Undo
      </button>
    </div>
  );
}

/** What to do next while drawing. */
export function DrawHint({ tool, points, busy, onDone, onBack, onCancel }: { tool: Tool; points: number; busy: boolean; onDone: () => void; onBack: () => void; onCancel: () => void }) {
  const text =
    tool === "select"
      ? "Drag a box over the rooms and walls to pick. Shift-drag adds more."
      : tool === "split"
        ? points === 0
          ? "Click where the missing wall starts"
          : "Click where it ends"
        : points < 3
          ? "Click each corner of the room"
          : "Keep clicking corners. Click the first corner or press Enter to finish.";
  return (
    <div className="panel flex items-center gap-3 whitespace-nowrap px-4 py-2 text-sm">
      <span>{text}</span>
      {tool === "draw" && points >= 3 && (
        <button className="btn-primary px-2 py-1 text-xs" disabled={busy} onClick={onDone}>
          Done
        </button>
      )}
      {tool !== "select" && points > 0 && (
        <button className="btn-ghost px-2 py-1 text-xs" onClick={onBack} title="Remove the last point (Backspace)">
          Back
        </button>
      )}
      <button className="btn-ghost px-1.5 py-1 text-xs" onClick={onCancel} aria-label="Stop">
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
