import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Combine, Trash2 } from "lucide-react";
import clsx from "clsx";
import { api } from "../lib/api";
import type { FloorSummary, RoomKind } from "../lib/types";
import { KIND_COLORS } from "./MapView";
import { Modal, Spinner } from "./ui";
import { useToast } from "./Toast";

const KINDS: RoomKind[] = ["room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown"];

/** Rename a floor or change which level it is. */
export function EditFloorDialog({ floor, onClose }: { floor: FloorSummary; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState(floor.name);
  const [level, setLevel] = useState(String(floor.level));
  const save = useMutation({
    mutationFn: () => api.updateFloor(floor.id, { name: name.trim() || floor.name, level: Number(level) }),
    onSuccess: () => {
      qc.invalidateQueries();
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't save", e.message),
  });
  const bad = level.trim() === "" || Number.isNaN(Number(level));
  return (
    <Modal title={`Edit ${floor.name}`} onClose={onClose}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (!bad) save.mutate();
        }}
      >
        <div>
          <label className="label">Name</label>
          <input autoFocus className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <label className="label">Level</label>
          <input className="input w-28" value={level} onChange={(e) => setLevel(e.target.value)} />
          <p className="mt-1 text-xs text-slate-500">0 is the ground floor, 1 the floor above, -1 the basement. Use 1.5 for a mezzanine. If another plan is on that level, the two swap.</p>
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" disabled={bad || save.isPending}>
            {save.isPending && <Spinner />} Save
          </button>
        </div>
      </form>
    </Modal>
  );
}

interface RoomRow {
  room_id: string;
  label: string | null;
  kind: RoomKind;
  area_m2: number;
}

/** Every room on a floor in a list: fix numbers and types, and select several (shift-click for a range) to merge. */
export function RoomsDialog({ floor, onClose }: { floor: FloorSummary; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const detail = useQuery({ queryKey: ["floor", floor.id], queryFn: () => api.floor(floor.id) });
  const rooms: RoomRow[] = useMemo(
    () =>
      (detail.data?.geojson?.features ?? [])
        .filter((f) => f.properties?.feature === "room")
        .map((f) => f.properties as RoomRow)
        .sort((a, b) => (a.label ?? "￿").localeCompare(b.label ?? "￿", undefined, { numeric: true })),
    [detail.data],
  );
  const [selected, setSelected] = useState<string[]>([]);
  const anchor = useRef<number | null>(null);
  const [mergeLabel, setMergeLabel] = useState("");

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["floor", floor.id] });
    qc.invalidateQueries({ queryKey: ["level"] });
    qc.invalidateQueries({ queryKey: ["building"] });
  };
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { label?: string | null; kind?: RoomKind } }) => api.updateRoom(floor.id, id, body),
    onSuccess: refresh,
    onError: (e: Error) => toast.error("Couldn't save", e.message),
  });
  const remove = useMutation({ mutationFn: (id: string) => api.deleteRoom(floor.id, id), onSuccess: refresh });
  const merge = useMutation({
    mutationFn: () => api.mergeRooms(floor.id, { room_ids: selected, label: mergeLabel.trim() || null }),
    onSuccess: () => {
      toast.success(`Merged ${selected.length} rooms`);
      setSelected([]);
      setMergeLabel("");
      qc.invalidateQueries();
    },
    onError: (e: Error) => toast.error("Couldn't merge", e.message),
  });

  const toggle = (i: number, shift: boolean) => {
    const id = rooms[i].room_id;
    if (shift && anchor.current !== null) {
      const [a, b] = [Math.min(anchor.current, i), Math.max(anchor.current, i)];
      const range = rooms.slice(a, b + 1).map((r) => r.room_id);
      setSelected((s) => [...new Set([...s, ...range])]);
    } else {
      setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
    }
    anchor.current = i;
  };
  useEffect(() => {
    if (selected.length >= 2 && !mergeLabel) setMergeLabel(rooms.find((r) => selected.includes(r.room_id) && r.label)?.label ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected.length]);

  return (
    <Modal title={`Rooms on ${floor.name}`} onClose={onClose} wide>
      {detail.isLoading && <Spinner />}
      {!detail.isLoading && rooms.length === 0 && <div className="text-sm text-slate-500">No rooms found on this floor yet.</div>}
      {rooms.length > 0 && (
        <>
          <p className="mb-2 text-xs text-slate-500">
            Click a number to change it. Tick rooms to merge them; shift-click a tick to select a whole range.
          </p>
          <div className="max-h-[55vh] overflow-auto rounded-lg border border-slate-200">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="w-8 px-2 py-1.5" />
                  <th className="px-2 py-1.5">Number or name</th>
                  <th className="px-2 py-1.5">Type</th>
                  <th className="px-2 py-1.5 text-right">Area</th>
                  <th className="w-8" />
                </tr>
              </thead>
              <tbody>
                {rooms.map((r, i) => (
                  <RoomRowView
                    key={r.room_id}
                    room={r}
                    checked={selected.includes(r.room_id)}
                    onCheck={(shift) => toggle(i, shift)}
                    onSave={(body) => patch.mutate({ id: r.room_id, body })}
                    onDelete={() => remove.mutate(r.room_id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <div className={clsx("mt-3 flex flex-wrap items-center gap-2", selected.length < 2 && "opacity-50")}>
            <span className="text-sm text-slate-600">{selected.length} selected</span>
            <input
              className="input w-48 py-1.5 text-sm"
              placeholder="Merged room number"
              value={mergeLabel}
              disabled={selected.length < 2}
              onChange={(e) => setMergeLabel(e.target.value)}
            />
            <button className="btn-primary px-3 py-1.5 text-xs" disabled={selected.length < 2 || merge.isPending} onClick={() => merge.mutate()}>
              {merge.isPending ? <Spinner /> : <Combine className="h-3.5 w-3.5" />} Merge into one room
            </button>
            {selected.length > 0 && (
              <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setSelected([])}>
                Clear
              </button>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}

function RoomRowView({
  room,
  checked,
  onCheck,
  onSave,
  onDelete,
}: {
  room: RoomRow;
  checked: boolean;
  onCheck: (shift: boolean) => void;
  onSave: (body: { label?: string | null; kind?: RoomKind }) => void;
  onDelete: () => void;
}) {
  const [label, setLabel] = useState(room.label ?? "");
  useEffect(() => setLabel(room.label ?? ""), [room.label]);
  return (
    <tr className={clsx("border-t border-slate-100", checked && "bg-indigo-50/60")}>
      <td className="px-2 py-1">
        <input
          type="checkbox"
          className="accent-indigo-600"
          checked={checked}
          onChange={() => {}}
          onClick={(e) => onCheck(e.shiftKey)}
          aria-label="Select for merging"
        />
      </td>
      <td className="px-2 py-1">
        <input
          className="w-full rounded border border-transparent bg-transparent px-1 py-0.5 hover:border-slate-200 focus:border-indigo-300 focus:bg-white focus:outline-none"
          value={label}
          placeholder="Unnumbered"
          onChange={(e) => setLabel(e.target.value)}
          onBlur={() => (label.trim() || null) !== (room.label ?? null) && onSave({ label: label.trim() || null })}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
      </td>
      <td className="px-2 py-1">
        <div className="flex items-center gap-1.5">
          <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: KIND_COLORS[room.kind] }} />
          <select className="rounded bg-transparent text-xs capitalize" value={room.kind} onChange={(e) => onSave({ kind: e.target.value as RoomKind })}>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>
      </td>
      <td className="px-2 py-1 text-right text-xs tabular-nums text-slate-500">{room.area_m2?.toLocaleString()} m²</td>
      <td className="px-1 py-1">
        <button className="p-1 text-slate-300 hover:text-rose-500" title="Not a room" onClick={() => confirm("Remove this room?") && onDelete()}>
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </td>
    </tr>
  );
}
