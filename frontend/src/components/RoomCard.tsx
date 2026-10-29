import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import type { RoomKind } from "../lib/types";
import type { RoomClick } from "./MapView";
import { KIND_COLORS } from "./MapView";
import { useToast } from "./Toast";

const KINDS: RoomKind[] = ["room", "corridor", "stair", "elevator", "restroom", "outdoor", "unknown"];

/** Details for the clicked room, with quick fixes for the converter's mistakes. */
export default function RoomCard({ room, buildingName, onClose }: { room: RoomClick; buildingName?: string; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const p = room.feature.properties as { floor_id: string; room_id: string; label: string | null; kind: RoomKind; area_m2: number; level: number };
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
        <div>
          <div className="text-base font-semibold">{p.label || "Unlabelled room"}</div>
          <div className="text-xs text-slate-500">
            {buildingName} · {p.area_m2?.toLocaleString()} m²
          </div>
        </div>
        <button className="btn-ghost -mr-2 -mt-1 p-1" onClick={onClose}>
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="mt-3 space-y-2">
        <div>
          <label className="label">Label</label>
          <input className="input" value={label} placeholder="e.g. 2310 or Great Hall" onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div>
          <label className="label">Type</label>
          <div className="flex flex-wrap gap-1">
            {KINDS.map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs capitalize ${kind === k ? "border-indigo-400 bg-indigo-50 text-indigo-700" : "border-slate-200 text-slate-600"}`}
              >
                <span className="h-2 w-2 rounded-full" style={{ background: KIND_COLORS[k] }} />
                {k}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="mt-4 flex items-center gap-2">
        <button className="btn-danger px-2 py-1 text-xs" onClick={() => confirm("Remove this room? Use this for things the converter wrongly marked as a room.") && remove.mutate()}>
          <Trash2 className="h-3.5 w-3.5" /> Not a room
        </button>
        <div className="flex-1" />
        <button className="btn-primary px-3 py-1.5 text-xs" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          Save
        </button>
      </div>
    </div>
  );
}
