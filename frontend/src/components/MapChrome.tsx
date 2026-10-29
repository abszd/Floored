import { Box, Map as MapIcon, Moon, Satellite, Square } from "lucide-react";
import clsx from "clsx";
import type { Basemap, Hover } from "./MapView";
import { KIND_COLORS, MAPBOX_TOKEN } from "./MapView";
import { levelShort } from "../lib/placement";

/** Basemap picker and 2D/3D switch, bottom-right above the zoom buttons. */
export function MapControls({
  basemap,
  onBasemap,
  threeD,
  onThreeD,
}: {
  basemap: Basemap;
  onBasemap: (b: Basemap) => void;
  threeD: boolean;
  onThreeD: (v: boolean) => void;
}) {
  const options: { key: Basemap; icon: typeof MapIcon; label: string }[] = [
    { key: "streets", icon: MapIcon, label: "Map" },
    { key: "satellite", icon: Satellite, label: "Satellite" },
    { key: "dark", icon: Moon, label: "Night" },
  ];
  return (
    <div className="flex flex-col items-end gap-2">
      <button
        onClick={() => onThreeD(!threeD)}
        className="panel flex items-center gap-1.5 px-3 py-2 text-xs font-semibold text-slate-700 hover:text-indigo-600"
        title={threeD ? "Flat view" : "See the floors stacked in 3D"}
      >
        {threeD ? <Square className="h-4 w-4" /> : <Box className="h-4 w-4" />}
        {threeD ? "2D" : "3D"}
      </button>
      {MAPBOX_TOKEN && (
        <div className="panel flex p-1">
          {options.map(({ key, icon: Icon, label }) => (
            <button
              key={key}
              onClick={() => onBasemap(key)}
              title={label}
              className={clsx(
                "flex h-8 w-8 items-center justify-center rounded-lg transition",
                basemap === key ? "bg-indigo-600 text-white" : "text-slate-500 hover:bg-slate-100",
              )}
            >
              <Icon className="h-4 w-4" />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Small label that follows the cursor over rooms and buildings. */
export function HoverTip({ hover }: { hover: Hover | null }) {
  if (!hover) return null;
  const p = hover.props;
  return (
    <div
      className="pointer-events-none absolute z-20 -translate-x-1/2 rounded-lg bg-slate-900/90 px-2.5 py-1.5 text-xs text-white shadow-lg"
      style={{ left: hover.x, top: hover.y - 44 }}
    >
      {hover.kind === "room" ? (
        <div className="flex items-center gap-1.5 whitespace-nowrap">
          <span className="h-2 w-2 rounded-full" style={{ background: KIND_COLORS[String(p.kind)] ?? KIND_COLORS.unknown }} />
          <span className="font-medium">{(p.label as string) || "Unlabelled"}</span>
          <span className="text-slate-300">
            · {String(p.kind ?? "room")}
            {p.area_m2 ? ` · ${Math.round(Number(p.area_m2))} m²` : ""}
            {p.level != null ? ` · level ${levelShort(Number(p.level))}` : ""}
          </span>
        </div>
      ) : (
        <div className="whitespace-nowrap">
          <span className="font-medium">{String(p.name)}</span>
          <span className="text-slate-300"> · {Number(p.floor_count) ? `${p.floor_count} floor${Number(p.floor_count) > 1 ? "s" : ""}` : "no floors yet"}</span>
        </div>
      )}
    </div>
  );
}

/** Mapbox GL won't draw anything without a token, so explain instead of showing a blank map. */
export function TokenNotice() {
  if (MAPBOX_TOKEN) return null;
  return (
    <div className="panel pointer-events-auto max-w-sm p-5 text-sm text-slate-600">
      <div className="mb-1 font-semibold text-slate-900">The map needs a Mapbox token</div>
      Set <code className="rounded bg-slate-100 px-1">VITE_MAPBOX_TOKEN</code> to a public token (it starts with{" "}
      <code className="rounded bg-slate-100 px-1">pk.</code>) and rebuild. Everything else still works from the panel.
    </div>
  );
}
