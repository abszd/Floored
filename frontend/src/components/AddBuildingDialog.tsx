import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, MapPin, MousePointerClick, Search } from "lucide-react";
import { api } from "../lib/api";
import type { Building, Place } from "../lib/types";
import { Modal, Spinner } from "./ui";
import { useToast } from "./Toast";

/** Find a building by address or name; buildings with an OSM outline are marked, since they place best. */
export default function AddBuildingDialog({
  onClose,
  onCreated,
  onPickOnMap,
}: {
  onClose: () => void;
  onCreated: (b: Building) => void;
  onPickOnMap: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<Place | null>(null);

  const results = useQuery({ queryKey: ["geocode", submitted], queryFn: () => api.geocode(submitted), enabled: submitted.length > 1 });

  const create = useMutation({
    mutationFn: (p: Place) => api.createBuilding({ name: name.trim() || undefined, place: p }),
    onSuccess: (b) => {
      qc.invalidateQueries();
      toast.success(`Added ${b.name}`);
      onCreated(b);
    },
    onError: (e: Error) => toast.error("Couldn't add building", e.message),
  });

  return (
    <Modal title="Add a building" onClose={onClose}>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(q.trim());
          setChosen(null);
        }}
      >
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input autoFocus className="input pl-9" placeholder="Address or building name" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button className="btn-primary" disabled={q.trim().length < 2}>
          Search
        </button>
      </form>

      <div className="mt-4 space-y-1.5">
        {results.isFetching && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <Spinner /> Searching OpenStreetMap…
          </div>
        )}
        {results.error && <div className="text-sm text-rose-600">{(results.error as Error).message}</div>}
        {results.data?.length === 0 && <div className="text-sm text-slate-500">Nothing found. Try adding the city.</div>}
        {results.data?.map((p) => (
          <button
            key={`${p.osm_id}-${p.lat}`}
            onClick={() => {
              setChosen(p);
              if (!name) setName(p.name);
            }}
            className={`flex w-full items-start gap-3 rounded-lg border px-3 py-2 text-left text-sm transition ${chosen === p ? "border-indigo-400 bg-indigo-50" : "border-slate-200 hover:bg-slate-50"}`}
          >
            {p.footprint ? <Building2 className="mt-0.5 h-4 w-4 shrink-0 text-indigo-600" /> : <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />}
            <div className="min-w-0">
              <div className="font-medium">{p.name || p.display_name.split(",")[0]}</div>
              <div className="truncate text-xs text-slate-500">{p.display_name}</div>
              {p.footprint && <div className="mt-0.5 text-[11px] font-medium text-indigo-600">Has building outline</div>}
            </div>
          </button>
        ))}
      </div>

      {chosen && (
        <div className="mt-4">
          <label className="label">Name on the map</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
      )}

      <div className="mt-5 flex items-center gap-2">
        <button className="btn-ghost" onClick={onPickOnMap}>
          <MousePointerClick className="h-4 w-4" /> Click it on the map instead
        </button>
        <div className="flex-1" />
        <button className="btn-primary" disabled={!chosen || create.isPending} onClick={() => chosen && create.mutate(chosen)}>
          {create.isPending && <Spinner />} Add building
        </button>
      </div>
    </Modal>
  );
}
