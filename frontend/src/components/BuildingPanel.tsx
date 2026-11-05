import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, ExternalLink, Image as ImageIcon, ListOrdered, Minus, Move, MoreHorizontal, Pencil, Plus, RefreshCw, Shapes, Trash2, Layers } from "lucide-react";
import { api, mediaUrl } from "../lib/api";
import { useJobs } from "../lib/jobs";
import { levelShort } from "../lib/placement";
import type { Building, FloorSummary } from "../lib/types";
import AddFloorDialog from "./AddFloorDialog";
import EditBuildingDialog from "./EditBuildingDialog";
import { EditFloorDialog, RoomsDialog } from "./FloorDialogs";
import { Empty, Modal, StatusBadge } from "./ui";
import { useToast } from "./Toast";

interface Props {
  building: Building;
  level: number | null;
  onLevel: (level: number) => void;
  onBack: () => void;
  onEditPlacement: (floorId: string) => void;
  /** Click the map to add an OSM outline to this building, or take a part away. */
  onPickOutline: (mode: "add" | "remove") => void;
}

export default function BuildingPanel({ building, level, onLevel, onBack, onEditPlacement, onPickOutline }: Props) {
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [preview, setPreview] = useState<FloorSummary | null>(null);
  const floors = [...building.floors].sort((a, b) => b.level - a.level);

  const spread = useMutation({
    mutationFn: () => api.spreadLevels(building.id),
    onSuccess: ({ moved }) => {
      qc.invalidateQueries();
      toast.success(`Moved ${moved} plan${moved === 1 ? "" : "s"} to levels of their own`, "Check the floors marked “check level”.");
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteBuilding(building.id),
    onSuccess: () => {
      qc.invalidateQueries();
      toast.success(`Removed ${building.name}`);
      onBack();
    },
  });

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-100 p-4">
        <button className="btn-ghost -ml-2 mb-2 px-2 py-1 text-xs" onClick={onBack}>
          <ArrowLeft className="h-3.5 w-3.5" /> All buildings
        </button>
        <div className="group flex items-start gap-2">
          <h1 className="text-lg font-semibold leading-tight">{building.name}</h1>
          <button className="mt-0.5 text-slate-400 hover:text-slate-600" onClick={() => setEditing(true)} title="Edit name and address">
            <Pencil className="h-3.5 w-3.5" />
          </button>
        </div>
        <button className="mt-1 block text-left text-sm text-slate-500 hover:text-slate-700" onClick={() => setEditing(true)}>
          {building.address || <span className="text-indigo-600">Add an address</span>}
        </button>
        {!building.has_footprint && (
          <div className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
            No building outline from OpenStreetMap, so plans can't snap into place automatically. You can still place them by hand.
          </div>
        )}
        <div className="mt-2 flex items-center gap-1.5 text-xs text-slate-500">
          <Shapes className="h-3.5 w-3.5 shrink-0" />
          <span className="flex-1">
            {building.has_footprint ? `Outline: ${building.outline_parts > 1 ? `${building.outline_parts} separate parts` : "one shape"}` : "No outline"}
          </span>
          <button className="btn-ghost px-1.5 py-0.5 text-[11px]" onClick={() => onPickOutline("add")} title="Click another OpenStreetMap shape to make it part of this building">
            <Plus className="h-3 w-3" /> Add part
          </button>
          {building.outline_parts > 1 && (
            <button className="btn-ghost px-1.5 py-0.5 text-[11px]" onClick={() => onPickOutline("remove")} title="Click a part of the outline to take it away">
              <Minus className="h-3 w-3" /> Remove part
            </button>
          )}
        </div>
        {building.shared_levels > 0 && (
          <div className="mt-2 flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <div className="flex-1">
              {building.shared_levels} plan{building.shared_levels > 1 ? "s share a level" : " shares a level"} with another, so they overlap on the map.
              <button className="ml-1 font-semibold underline" disabled={spread.isPending} onClick={() => spread.mutate()}>
                Give each its own level
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="flex items-center justify-between px-4 pb-2 pt-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Floors</h2>
        <button className="btn-primary px-2.5 py-1.5 text-xs" onClick={() => setAdding(true)}>
          <Plus className="h-3.5 w-3.5" /> Add floor plan
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-4">
        {floors.length === 0 && (
          <Empty icon={<Layers className="h-5 w-5" />} title="No floor plans yet">
            Upload one, or find them automatically from the Discover page.
          </Empty>
        )}
        {floors.map((f) => (
          <FloorRow
            key={f.id}
            floor={f}
            active={level === f.level}
            onClick={() => onLevel(f.level)}
            onEdit={() => onEditPlacement(f.id)}
            onPreview={() => setPreview(f)}
          />
        ))}
      </div>

      <div className="border-t border-slate-100 p-3">
        <button
          className="btn-danger w-full justify-start text-xs"
          onClick={() => confirm(`Remove ${building.name} and all its floors?`) && remove.mutate()}
        >
          <Trash2 className="h-3.5 w-3.5" /> Remove building
        </button>
      </div>

      {adding && <AddFloorDialog building={building} onClose={() => setAdding(false)} />}
      {editing && <EditBuildingDialog building={building} onClose={() => setEditing(false)} />}
      {preview && <FloorImages floorId={preview.id} title={preview.name} onClose={() => setPreview(null)} />}
    </div>
  );
}

function FloorRow({
  floor,
  active,
  onClick,
  onEdit,
  onPreview,
}: {
  floor: FloorSummary;
  active: boolean;
  onClick: () => void;
  onEdit: () => void;
  onPreview: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track } = useJobs();
  const [menu, setMenu] = useState(false);
  const [editing, setEditing] = useState(false);
  const [rooms, setRooms] = useState(false);
  const reprocess = useMutation({
    mutationFn: () => api.reprocess(floor.id),
    onSuccess: ({ job_id }) => track(job_id, `${floor.name} conversion`),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteFloor(floor.id),
    onSuccess: () => {
      qc.invalidateQueries();
      toast.success(`Removed ${floor.name}`);
    },
  });
  const busy = floor.status === "pending" || floor.status === "processing";

  return (
    <div
      onClick={onClick}
      className={`group relative mb-1 flex cursor-pointer items-center gap-3 rounded-xl px-2 py-2 transition ${active ? "bg-indigo-50 ring-1 ring-indigo-200" : "hover:bg-slate-50"}`}
    >
      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-sm font-semibold ${active ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-600"}`}>
        {levelShort(floor.level)}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{floor.name}</div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 whitespace-nowrap">
          <StatusBadge status={floor.status} />
          {floor.level_note && (
            <button
              className="inline-flex items-center rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700"
              title={floor.level_note}
              onClick={(e) => {
                e.stopPropagation();
                setEditing(true);
              }}
            >
              Check level
            </button>
          )}
          {floor.room_count > 0 && <span className="text-xs text-slate-400">{floor.room_count} rooms</span>}
        </div>
        {floor.status === "error" && floor.error && <div className="mt-1 line-clamp-2 text-xs text-rose-600">{floor.error}</div>}
      </div>
      {floor.status === "needs_review" && (
        <button
          className="btn-secondary px-2 py-1 text-xs"
          onClick={(e) => {
            e.stopPropagation();
            onEdit();
          }}
        >
          Review
        </button>
      )}
      <button
        className="rounded-md p-1 text-slate-400 opacity-0 transition hover:bg-slate-200 hover:text-slate-600 group-hover:opacity-100"
        onClick={(e) => {
          e.stopPropagation();
          setMenu((m) => !m);
        }}
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
      {menu && (
        <div className="panel absolute right-2 top-11 z-10 w-48 p-1 text-sm" onMouseLeave={() => setMenu(false)} onClick={(e) => e.stopPropagation()}>
          <MenuItem icon={<Pencil className="h-4 w-4" />} onClick={() => (setMenu(false), setEditing(true))}>
            Rename or change level
          </MenuItem>
          <MenuItem icon={<ListOrdered className="h-4 w-4" />} disabled={!floor.room_count} onClick={() => (setMenu(false), setRooms(true))}>
            Edit room numbers
          </MenuItem>
          <MenuItem icon={<Move className="h-4 w-4" />} disabled={busy || floor.status === "error"} onClick={() => (setMenu(false), onEdit())}>
            Adjust placement
          </MenuItem>
          <MenuItem icon={<ImageIcon className="h-4 w-4" />} onClick={() => (setMenu(false), onPreview())}>
            View plan images
          </MenuItem>
          <MenuItem icon={<RefreshCw className="h-4 w-4" />} disabled={busy} onClick={() => (setMenu(false), reprocess.mutate())}>
            Find rooms again
          </MenuItem>
          {floor.source_url && (
            <MenuItem icon={<ExternalLink className="h-4 w-4" />} onClick={() => window.open(floor.source_url!, "_blank")}>
              Open source
            </MenuItem>
          )}
          <MenuItem icon={<Trash2 className="h-4 w-4" />} danger onClick={() => (setMenu(false), confirm(`Remove ${floor.name}?`) && remove.mutate())}>
            Remove floor
          </MenuItem>
        </div>
      )}
      {editing && (
        <div onClick={(e) => e.stopPropagation()}>
          <EditFloorDialog floor={floor} onClose={() => setEditing(false)} />
        </div>
      )}
      {rooms && (
        <div onClick={(e) => e.stopPropagation()}>
          <RoomsDialog floor={floor} onClose={() => setRooms(false)} />
        </div>
      )}
    </div>
  );
}

function MenuItem({ icon, children, onClick, disabled, danger }: { icon: React.ReactNode; children: React.ReactNode; onClick: () => void; disabled?: boolean; danger?: boolean }) {
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left disabled:opacity-40 ${danger ? "text-rose-600 hover:bg-rose-50" : "hover:bg-slate-50"}`}
    >
      {icon}
      {children}
    </button>
  );
}

function FloorImages({ floorId, title, onClose }: { floorId: string; title: string; onClose: () => void }) {
  const [tab, setTab] = useState("original");
  const [data, setData] = useState<Awaited<ReturnType<typeof api.floor>> | null>(null);
  if (!data) api.floor(floorId).then(setData);
  const images: Record<string, string | undefined> = { original: mediaUrl(data?.image_url), ...Object.fromEntries(Object.entries(data?.debug_images ?? {}).map(([k, v]) => [k, mediaUrl(v)])) };
  return (
    <Modal title={title} onClose={onClose} wide>
      <div className="mb-3 inline-flex rounded-lg bg-slate-100 p-1 text-sm">
        {Object.keys(images).map((k) => (
          <button key={k} onClick={() => setTab(k)} className={`rounded-md px-3 py-1 capitalize ${tab === k ? "bg-white font-medium shadow-sm" : "text-slate-500"}`}>
            {k}
          </button>
        ))}
      </div>
      {images[tab] ? <img src={images[tab]} className="max-h-[65vh] w-full rounded-lg border border-slate-100 object-contain" /> : <div className="h-64 animate-pulse rounded-lg bg-slate-100" />}
      {data?.converter && <div className="mt-2 text-xs text-slate-400">Converted with {data.converter}</div>}
    </Modal>
  );
}
