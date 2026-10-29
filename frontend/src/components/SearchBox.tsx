import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Building2, DoorOpen, Search, X } from "lucide-react";
import { api } from "../lib/api";
import type { SearchResult } from "../lib/types";
import { levelShort } from "../lib/placement";

export default function SearchBox({
  onBuilding,
  onRoom,
}: {
  onBuilding: (id: string) => void;
  onRoom: (r: SearchResult["rooms"][number]) => void;
}) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 200);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => {
    const close = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const { data } = useQuery({ queryKey: ["search", debounced], queryFn: () => api.search(debounced), enabled: debounced.length > 0 });
  const empty = data && data.buildings.length === 0 && data.rooms.length === 0;

  return (
    <div ref={box} className="relative">
      <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
      <input
        className="input pl-9 pr-8"
        placeholder="Search buildings and rooms"
        value={q}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
      />
      {q && (
        <button className="absolute right-2 top-2 rounded p-0.5 text-slate-400 hover:text-slate-600" onClick={() => setQ("")}>
          <X className="h-4 w-4" />
        </button>
      )}
      {open && debounced && data && (
        <div className="panel absolute left-0 right-0 top-full z-20 mt-1 max-h-80 overflow-auto p-1">
          {empty && <div className="px-3 py-2 text-sm text-slate-500">No matches</div>}
          {data.buildings.map((b) => (
            <button
              key={b.id}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-slate-50"
              onClick={() => {
                onBuilding(b.id);
                setOpen(false);
              }}
            >
              <Building2 className="h-4 w-4 text-indigo-500" />
              <span className="font-medium">{b.name}</span>
              <span className="truncate text-xs text-slate-400">{b.address}</span>
            </button>
          ))}
          {data.rooms.map((r) => (
            <button
              key={`${r.floor_id}-${r.room_id}`}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-slate-50"
              onClick={() => {
                onRoom(r);
                setOpen(false);
              }}
            >
              <DoorOpen className="h-4 w-4 text-emerald-500" />
              <span className="font-medium">{r.label}</span>
              <span className="truncate text-xs text-slate-400">
                {r.building_name} · level {levelShort(r.level)}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
