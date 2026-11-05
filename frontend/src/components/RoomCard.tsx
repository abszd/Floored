import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Combine, Scissors, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import type { RoomKind } from "../lib/types";
import type { RoomClick } from "./MapView";
import { KIND_COLORS } from "./MapView";
import { Spinner } from "./ui";
import { useToast } from "./Toast";

const KINDS: RoomKind[] = ["room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown"];

type RoomProps = { floor_id: string; room_id: string; label: string | null; kind: RoomKind; area_m2: number; level: number };

function KindPicker({ kind, onChange }: { kind: RoomKind; onChange: (k: RoomKind) => void }) {
  return (
    <div className="flex flex-wrap gap-1">
      {KINDS.map((k) => (
        <button
          key={k}
          onClick={() => onChange(k)}
          className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs capitalize ${kind === k ? "border-indigo-400 bg-indigo-50 text-indigo-700" : "border-slate-200 text-slate-600"}`}
        >
          <span className="h-2 w-2 rounded-full" style={{ background: KIND_COLORS[k] }} />
          {k}
        </button>
      ))}
    </div>
  );
}

/** Details for the clicked room, with quick fixes for the converter's mistakes. */
export default function RoomCard({
  room,
  buildingName,
  onClose,
  onSplit,
}: {
  room: RoomClick;
  buildingName?: string;
  onClose: () => void;
  /** Start drawing a line to cut this room in two. */
  onSplit?: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const p = room.feature.properties as RoomProps;
  const [label, setLabel] = useState(p.label ?? "");
  const [kind, setKind] = useState<RoomKind>(p.kind ?? "unknown");
  useEffect(() => {
    setLabel(p.label ?? "");
    setKind(p.kind ?? "unknown");
  }, [p.room_id, p.floor_id, p.label, p.kind]);

  const save = useMutation({
    mutationFn: () => api.updateRoom(p.floor_id, p.room_id, { label: label.trim() || null, kind }),
    onSuccess: () => {
      qc.invalidateQueries();
      toast.success("Room updated");
    },
    onError: (e: Error) => toast.error("Couldn't update room", e.message),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteRoom(p.floor_id, p.room_id),
    onSuccess: () => {
      qc.invalidateQueries();
      onClose();
    },
  });
  const dirty = (label.trim() || null) !== (p.label ?? null) || kind !== p.kind;

  return (
    <div className="panel w-72 p-4">
      <div className="flex items-start justify-between">
        <div className="min-w-0 flex-1">
          <label className="label">Room number or name</label>
          <input
            autoFocus={!p.label}
            className="input text-base font-semibold"
            value={label}
            placeholder="e.g. 2310 or Great Hall"
            onChange={(e) => setLabel(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && dirty && save.mutate()}
          />
          <div className="mt-1 text-xs text-slate-500">
            {buildingName} · {p.area_m2?.toLocaleString()} m²
          </div>
        </div>
        <button className="btn-ghost -mr-2 -mt-1 ml-1 p-1" onClick={onClose} aria-label="Close">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="mt-3">
        <label className="label">Type</label>
        <KindPicker kind={kind} onChange={setKind} />
      </div>
      <p className="mt-3 text-[11px] text-slate-400">Shift-click other rooms, or shift-drag a box, to merge them into this one.</p>
      <div className="mt-3 flex items-center gap-2">
        <button className="btn-danger px-2 py-1 text-xs" onClick={() => confirm("Remove this room? Use this for things the converter wrongly marked as a room.") && remove.mutate()}>
          <Trash2 className="h-3.5 w-3.5" /> Not a room
        </button>
        {onSplit && (
          <button className="btn-secondary px-2 py-1 text-xs" onClick={onSplit} title="Draw a line where the converter missed a wall">
            <Scissors className="h-3.5 w-3.5" /> Split
          </button>
        )}
        <div className="flex-1" />
        <button className="btn-primary px-3 py-1.5 text-xs" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          Save
        </button>
      </div>
    </div>
  );
}

/** Several rooms (and walls) picked with shift-click or an area: merge the rooms, or remove the lot. */
export function MergeRoomsCard({
  floorId,
  rooms,
  walls = [],
  onRemove,
  onMerged,
  onDeleted,
  onDropWalls,
  onClose,
}: {
  floorId: string;
  rooms: RoomClick[];
  /** Map ids of picked walls and doors ("<floor>:wall3"). */
  walls?: string[];
  onRemove: (r: RoomClick) => void;
  onMerged: (floorId: string, roomId: string) => void;
  onDeleted: () => void;
  onDropWalls?: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const props = rooms.map((r) => r.feature.properties as RoomProps);
  const firstLabel = props.find((p) => p.label)?.label ?? "";
  const [label, setLabel] = useState(firstLabel);
  const [kind, setKind] = useState<RoomKind>(props[0]?.kind ?? "room");
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (!touched) setLabel(firstLabel);
  }, [firstLabel, touched]);
  const area = props.reduce((a, p) => a + (p.area_m2 ?? 0), 0);
  const index = (kind: string) =>
    walls.map((w) => w.split(":").pop() ?? "").filter((w) => w.startsWith(kind)).map((w) => Number(w.slice(kind.length)));
  const wallCount = index("wall").length;
  const doorCount = index("door").length;

  const merge = useMutation({
    mutationFn: () => api.mergeRooms(floorId, { room_ids: props.map((p) => p.room_id), label: label.trim() || null, kind }),
    onSuccess: async ({ room_id }) => {
      // Select the merged room only once the map has the new shapes.
      await qc.invalidateQueries();
      toast.success(`Merged ${rooms.length} rooms`);
      onMerged(floorId, room_id);
    },
    onError: (e: Error) => toast.error("Couldn't merge", e.message),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteFeatures(floorId, { room_ids: props.map((p) => p.room_id), walls: index("wall"), doors: index("door") }),
    onSuccess: async ({ removed }) => {
      await qc.invalidateQueries();
      const parts = [
        removed.rooms && `${removed.rooms} room${removed.rooms > 1 ? "s" : ""}`,
        removed.walls && `${removed.walls} wall${removed.walls > 1 ? "s" : ""}`,
        removed.doors && `${removed.doors} door${removed.doors > 1 ? "s" : ""}`,
      ].filter(Boolean);
      toast.success(`Removed ${parts.join(" and ")}`, "Press Ctrl+Z to undo.");
      onDeleted();
    },
    onError: (e: Error) => toast.error("Couldn't remove", e.message),
  });

  const title = [
    rooms.length && `${rooms.length} room${rooms.length > 1 ? "s" : ""}`,
    wallCount && `${wallCount} wall${wallCount > 1 ? "s" : ""}`,
    doorCount && `${doorCount} door${doorCount > 1 ? "s" : ""}`,
  ].filter(Boolean);

  return (
    <div className="panel w-72 p-4" data-testid="selection-card">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-base font-semibold">{title.join(", ")} selected</div>
          {rooms.length > 0 && <div className="text-xs text-slate-500">About {Math.round(area).toLocaleString()} m² of rooms</div>}
        </div>
        <button className="btn-ghost -mr-2 -mt-1 p-1" onClick={onClose} aria-label="Clear selection">
          <X className="h-4 w-4" />
        </button>
      </div>
      {rooms.length > 0 && (
        <div className="mt-2 flex max-h-24 flex-wrap gap-1 overflow-y-auto">
          {rooms.map((r, i) => (
            <span key={String(r.feature.id)} className="inline-flex items-center gap-1 rounded-full bg-indigo-50 px-2 py-0.5 text-xs text-indigo-700">
              {props[i].label || `Room ${i + 1}`}
              <button onClick={() => onRemove(r)} className="text-indigo-400 hover:text-indigo-700" aria-label="Remove from selection">
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      {walls.length > 0 && rooms.length > 0 && onDropWalls && (
        <button className="mt-1.5 text-[11px] text-slate-500 underline" onClick={onDropWalls}>
          Leave the walls out
        </button>
      )}
      {rooms.length >= 2 && (
        <div className="mt-3 space-y-2">
          <div>
            <label className="label">Merged room number or name</label>
            <input
              className="input"
              value={label}
              placeholder="e.g. Suite 200"
              onChange={(e) => {
                setTouched(true);
                setLabel(e.target.value);
              }}
            />
          </div>
          <div>
            <label className="label">Type</label>
            <KindPicker kind={kind} onChange={setKind} />
          </div>
        </div>
      )}
      <p className="mt-3 text-[11px] text-slate-400">Shift-click or shift-drag to add or take away.</p>
      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          className="btn-danger px-2 py-1 text-xs"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
          title="Remove everything selected. Ctrl+Z puts it back."
        >
          {remove.isPending ? <Spinner /> : <Trash2 className="h-3.5 w-3.5" />} Remove
        </button>
        <div className="flex-1" />
        {rooms.length >= 2 && (
          <button className="btn-primary px-3 py-1.5 text-xs" disabled={merge.isPending} onClick={() => merge.mutate()}>
            {merge.isPending ? <Spinner /> : <Combine className="h-3.5 w-3.5" />} Merge rooms
          </button>
        )}
      </div>
    </div>
  );
}
