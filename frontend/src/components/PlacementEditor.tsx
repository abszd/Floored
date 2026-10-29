import { useEffect, useRef, useState } from "react";
import mapboxgl, { type ImageSource, type Map as MBMap } from "mapbox-gl";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FlipHorizontal2, Magnet, RotateCw, Save, Undo2, X, Copy, Lock, Unlock } from "lucide-react";
import { api, mediaUrl } from "../lib/api";
import * as P from "../lib/placement";
import type { Building, Floor, Placement } from "../lib/types";
import { useToast } from "./Toast";
import { Spinner } from "./ui";

interface Props {
  map: MBMap;
  floor: Floor;
  building: Building;
  onClose: () => void;
}

function initialPlacement(floor: Floor, building: Building): Placement {
  if (floor.placement) return floor.placement;
  const [w, h] = floor.image_size ?? [1000, 1000];
  const span = 60;
  return {
    center_lon: building.lon,
    center_lat: building.lat,
    width_m: (span * w) / Math.max(w, h),
    height_m: (span * h) / Math.max(w, h),
    rotation_deg: 0,
    mirror: false,
  };
}

/**
 * Drag the plan image into place: move it by the centre handle, turn it by the
 * amber handle, resize it by the corners. This replaces the old two-marker
 * bounding box and adds rotation, which most real plans need.
 */
export default function PlacementEditor({ map, floor, building, onClose }: Props) {
  const qc = useQueryClient();
  const toast = useToast();
  const [placement, setPlacement] = useState<Placement>(() => initialPlacement(floor, building));
  const [opacity, setOpacity] = useState(0.75);
  const [uniform, setUniform] = useState(true);
  const [fitInfo, setFitInfo] = useState<{ iou: number | null; ambiguous: boolean } | null>(
    floor.fit_score != null ? { iou: floor.fit_score, ambiguous: floor.status === "needs_review" } : null,
  );
  const pRef = useRef(placement);
  pRef.current = placement;
  const uniformRef = useRef(uniform);
  uniformRef.current = uniform;
  const markers = useRef<{ move: mapboxgl.Marker; rotate: mapboxgl.Marker; corners: mapboxgl.Marker[] } | null>(null);

  // Image overlay + footprint highlight + handles.
  useEffect(() => {
    const url = mediaUrl(floor.image_url);
    if (!url) return;
    const absolute = new URL(url, window.location.href).toString();
    map.addSource("plan-image", { type: "image", url: absolute, coordinates: P.corners(pRef.current) });
    const slot = map.getStyle()?.imports?.length ? { slot: "middle" as const } : {};
    map.addLayer({ id: "plan-image", type: "raster", source: "plan-image", ...slot, paint: { "raster-opacity": opacity, "raster-fade-duration": 0 } });
    if (building.footprint) {
      map.addSource("plan-footprint", { type: "geojson", data: { type: "Feature", geometry: building.footprint, properties: {} } });
      map.addLayer({
        id: "plan-footprint",
        type: "line",
        source: "plan-footprint",
        ...slot,
        paint: { "line-color": "#f43f5e", "line-width": 2, "line-dasharray": [2, 1.5], "line-emissive-strength": 1 },
      });
    }

    const el = (cls: string, title: string) => {
      const d = document.createElement("div");
      d.className = `handle ${cls}`;
      d.title = title;
      return d;
    };
    const move = new mapboxgl.Marker({ element: el("handle-move", "Drag to move"), draggable: true })
      .setLngLat([pRef.current.center_lon, pRef.current.center_lat])
      .addTo(map);
    const rotate = new mapboxgl.Marker({ element: el("handle-rotate", "Drag to rotate"), draggable: true })
      .setLngLat(P.rotateHandle(pRef.current))
      .addTo(map);
    const corners = P.corners(pRef.current).map((c, i) => {
      const m = new mapboxgl.Marker({ element: el("", "Drag to resize"), draggable: true }).setLngLat(c).addTo(map);
      m.on("drag", () => {
        const { lng, lat } = m.getLngLat();
        setPlacement(P.withCornerAt(pRef.current, i, lng, lat, uniformRef.current));
      });
      return m;
    });
    move.on("drag", () => {
      const { lng, lat } = move.getLngLat();
      setPlacement(P.withCenter(pRef.current, lng, lat));
    });
    rotate.on("drag", () => {
      const { lng, lat } = rotate.getLngLat();
      setPlacement(P.withRotationToward(pRef.current, lng, lat));
    });
    markers.current = { move, rotate, corners };

    return () => {
      move.remove();
      rotate.remove();
      corners.forEach((m) => m.remove());
      markers.current = null;
      for (const id of ["plan-image", "plan-footprint"]) {
        if (map.getLayer(id)) map.removeLayer(id);
        if (map.getSource(id)) map.removeSource(id);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, floor.id, floor.image_url]);

  // Keep the overlay and handles in step with the placement.
  useEffect(() => {
    const src = map.getSource("plan-image") as ImageSource | undefined;
    src?.setCoordinates(P.corners(placement));
    const m = markers.current;
    if (!m) return;
    m.move.setLngLat([placement.center_lon, placement.center_lat]);
    m.rotate.setLngLat(P.rotateHandle(placement));
    P.corners(placement).forEach((c, i) => m.corners[i].setLngLat(c));
  }, [map, placement]);

  useEffect(() => {
    if (map.getLayer("plan-image")) map.setPaintProperty("plan-image", "raster-opacity", opacity);
  }, [map, opacity]);

  // Fly to the plan when the editor opens.
  useEffect(() => {
    const cs = P.corners(pRef.current);
    const b = new mapboxgl.LngLatBounds(cs[0], cs[0]);
    cs.forEach((c) => b.extend(c));
    map.fitBounds(b, { padding: { top: 80, bottom: 80, left: 420, right: 80 }, maxZoom: 19.5, duration: 600 });
  }, [map, floor.id]);

  const save = useMutation({
    mutationFn: () => api.savePlacement(floor.id, pRef.current),
    onSuccess: () => {
      qc.invalidateQueries();
      toast.success("Placement saved");
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't save placement", e.message),
  });

  const autofit = useMutation({
    mutationFn: () => api.autofit(floor.id),
    onSuccess: ({ floor: f, fit }) => {
      if (f.placement) setPlacement(f.placement);
      setFitInfo(fit);
      qc.invalidateQueries({ queryKey: ["building"] });
      if (fit.iou == null) toast.error("No building outline to fit to", "Place it by hand, or add an outline to the building.");
    },
    onError: (e: Error) => toast.error("Auto-fit failed", e.message),
  });

  const copyFrom = useMutation({
    mutationFn: (fromId: string) => api.copyPlacement(floor.id, fromId),
    onSuccess: (f) => f.placement && setPlacement(f.placement),
    onError: (e: Error) => toast.error("Couldn't copy placement", e.message),
  });

  const others = building.floors.filter((f) => f.id !== floor.id && (f.status === "placed" || f.status === "needs_review"));
  const num = (v: number, d = 1) => Math.round(v * 10 ** d) / 10 ** d;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-start justify-between border-b border-slate-100 p-4">
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-indigo-600">Adjust placement</div>
          <div className="text-base font-semibold">
            {building.name} · {floor.name}
          </div>
        </div>
        <button className="btn-ghost -mr-2 p-1.5" onClick={onClose} aria-label="Close editor">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        <p className="text-sm text-slate-500">
          Line the plan up with the dashed red building outline. Drag the <b className="text-slate-700">white centre</b> to
          move, the <b className="text-amber-600">amber handle</b> to rotate, and the corners to resize.
        </p>

        {fitInfo && fitInfo.iou != null && (
          <div className={`rounded-lg px-3 py-2 text-sm ${fitInfo.iou >= 0.75 && !fitInfo.ambiguous ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-800"}`}>
            Overlap with the building outline: <b>{Math.round(fitInfo.iou * 100)}%</b>
            {fitInfo.ambiguous && <div className="mt-0.5 text-xs">The building is symmetric, so check the plan isn't turned around. If it is, press “Rotate 90°” twice.</div>}
          </div>
        )}

        <div className="grid grid-cols-2 gap-2">
          <button className="btn-primary col-span-2" onClick={() => autofit.mutate()} disabled={autofit.isPending || !building.footprint}>
            {autofit.isPending ? <Spinner /> : <Magnet className="h-4 w-4" />}
            Snap to building outline
          </button>
          <button className="btn-secondary" onClick={() => setPlacement((p) => ({ ...p, rotation_deg: (p.rotation_deg + 90) % 360 }))}>
            <RotateCw className="h-4 w-4" /> Rotate 90°
          </button>
          <button className="btn-secondary" onClick={() => setPlacement((p) => ({ ...p, mirror: !p.mirror }))}>
            <FlipHorizontal2 className="h-4 w-4" /> Mirror
          </button>
        </div>
        {!building.footprint && (
          <p className="-mt-3 text-xs text-slate-500">This building has no outline from OpenStreetMap, so snapping is unavailable.</p>
        )}

        <div>
          <label className="label">Rotation (° from north)</label>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={0}
              max={360}
              step={0.5}
              value={placement.rotation_deg}
              onChange={(e) => setPlacement((p) => ({ ...p, rotation_deg: Number(e.target.value) }))}
              className="flex-1 accent-indigo-600"
            />
            <input
              type="number"
              className="input w-20"
              value={num(placement.rotation_deg)}
              onChange={(e) => setPlacement((p) => ({ ...p, rotation_deg: ((Number(e.target.value) % 360) + 360) % 360 }))}
            />
          </div>
        </div>

        <div>
          <div className="mb-1 flex items-center justify-between">
            <label className="label mb-0">Size of the whole image (m)</label>
            <button className="btn-ghost px-1.5 py-0.5 text-xs" onClick={() => setUniform((u) => !u)} title="Lock aspect ratio">
              {uniform ? <Lock className="h-3.5 w-3.5" /> : <Unlock className="h-3.5 w-3.5" />}
              {uniform ? "Locked" : "Free"}
            </button>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <input
              type="number"
              className="input"
              value={num(placement.width_m)}
              min={1}
              onChange={(e) => {
                const w = Math.max(1, Number(e.target.value));
                setPlacement((p) => (uniform ? P.scaled(p, w / p.width_m) : { ...p, width_m: w }));
              }}
            />
            <input
              type="number"
              className="input"
              value={num(placement.height_m)}
              min={1}
              onChange={(e) => {
                const h = Math.max(1, Number(e.target.value));
                setPlacement((p) => (uniform ? P.scaled(p, h / p.height_m) : { ...p, height_m: h }));
              }}
            />
          </div>
        </div>

        <div>
          <label className="label">Plan opacity</label>
          <input type="range" min={0.1} max={1} step={0.05} value={opacity} onChange={(e) => setOpacity(Number(e.target.value))} className="w-full accent-indigo-600" />
        </div>

        {others.length > 0 && (
          <div>
            <label className="label">Line up with another floor</label>
            <div className="flex flex-wrap gap-1.5">
              {others.map((o) => (
                <button key={o.id} className="btn-secondary px-2 py-1 text-xs" onClick={() => copyFrom.mutate(o.id)}>
                  <Copy className="h-3 w-3" /> {o.name}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex gap-2 border-t border-slate-100 p-4">
        <button className="btn-secondary" onClick={() => setPlacement(initialPlacement(floor, building))}>
          <Undo2 className="h-4 w-4" /> Reset
        </button>
        <div className="flex-1" />
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? <Spinner /> : <Save className="h-4 w-4" />} Save
        </button>
      </div>
    </div>
  );
}
