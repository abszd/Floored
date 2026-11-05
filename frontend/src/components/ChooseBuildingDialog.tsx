import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { MapPin } from "lucide-react";
import { api } from "../lib/api";
import type { Building, Place } from "../lib/types";
import AddressInput from "./AddressInput";
import PlacePreviewMap from "./PlacePreviewMap";
import { Modal, Spinner } from "./ui";
import { useToast } from "./Toast";

export type Pick = { building: Building; place?: undefined } | { place: Place; building?: undefined };

/** Pick a building already on the map, or find one on OpenStreetMap, previewing it before use. */
export default function ChooseBuildingDialog({
  name,
  address,
  hint,
  onPicked,
  onClose,
}: {
  name: string | null;
  address: string | null;
  hint: string | null;
  onPicked: (pick: Pick, address?: string) => Promise<unknown> | void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [q, setQ] = useState(name ?? "");
  const [geoQ, setGeoQ] = useState("");
  const [pick, setPick] = useState<Pick | null>(null);
  const [pickedAddress, setPickedAddress] = useState<string | undefined>();
  const buildings = useQuery({ queryKey: ["buildings"], queryFn: api.buildings });
  const places = useQuery({ queryKey: ["geocode", geoQ, hint], queryFn: () => api.geocode(geoQ, hint), enabled: geoQ.length > 1 });
  const filtered = useMemo(
    () => (buildings.data ?? []).filter((b) => !q || `${b.name} ${b.address ?? ""} ${b.aliases.join(" ")}`.toLowerCase().includes(q.toLowerCase())),
    [buildings.data, q],
  );

  const lookup = useMutation({
    mutationFn: (target: string | Place) => {
      setPickedAddress(typeof target === "string" ? target : target.display_name);
      return typeof target === "string"
        ? api.lookup({ name, address: target, hint })
        : api.lookup({ name, address: target.display_name, lat: target.lat, lon: target.lon });
    },
    onSuccess: (r) => setPick(r.building ? { building: r.building } : r.place ? { place: r.place } : null),
    onError: (e: Error) => toast.error("Couldn't find that address", e.message),
  });
  const pickBuilding = useMutation({
    mutationFn: (id: string) => api.building(id),
    onSuccess: (b) => setPick({ building: b }),
  });
  const confirm = useMutation({
    mutationFn: async (p: Pick) => onPicked(p, pickedAddress),
    onSuccess: onClose,
    onError: (e: Error) => toast.error("Couldn't set building", e.message),
  });

  const where = pick?.building ?? pick?.place;

  return (
    <Modal title="Which building is this?" onClose={onClose}>
      {address && (
        <button className="btn-secondary mb-3 w-full justify-start" disabled={lookup.isPending} onClick={() => lookup.mutate(address)}>
          {lookup.isPending ? <Spinner /> : <MapPin className="h-4 w-4" />}
          <span className="truncate">
            Use the address on the page: {address}
            {hint && !address.toLowerCase().includes(hint.toLowerCase()) ? ` (${hint})` : ""}
          </span>
        </button>
      )}

      {where && pick && (
        <div className="mb-4 space-y-2 rounded-xl border border-indigo-100 bg-indigo-50/40 p-3">
          <PlacePreviewMap place={where} />
          <div className="text-sm">
            <div className="font-medium">{pick.building ? pick.building.name : pick.place.name || pick.place.display_name.split(",")[0]}</div>
            <div className="text-xs text-slate-500">
              {pick.building ? `${pick.building.address ?? ""} · already on your map` : pick.place.display_name}
              {!where.footprint && " · no building outline, so its plans will need placing by hand"}
            </div>
          </div>
          <div className="flex gap-2">
            <button className="btn-primary" disabled={confirm.isPending} onClick={() => confirm.mutate(pick)}>
              {confirm.isPending && <Spinner />} Use this building
            </button>
            <button className="btn-ghost" onClick={() => setPick(null)}>
              Not this one
            </button>
          </div>
        </div>
      )}

      <input autoFocus className="input" placeholder="Filter your buildings" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="mt-2 max-h-56 space-y-1 overflow-auto">
        {filtered.map((b) => (
          <button key={b.id} onClick={() => pickBuilding.mutate(b.id)} className="flex w-full flex-col rounded-lg px-3 py-2 text-left hover:bg-slate-50">
            <span className="text-sm font-medium">{b.name}</span>
            <span className="truncate text-xs text-slate-500">{b.address}</span>
          </button>
        ))}
        {filtered.length === 0 && <div className="px-3 py-2 text-sm text-slate-500">None of your buildings match.</div>}
      </div>
      <div className="mt-4 border-t border-slate-100 pt-4">
        <label className="label">Or find it on OpenStreetMap{hint ? ` (results in ${hint} first)` : ""}</label>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setGeoQ(q.trim());
          }}
        >
          <AddressInput value={q} onChange={setQ} onPick={(p) => lookup.mutate(p)} near={{ hint }} placeholder="Start typing an address or building name" />
          <button className="btn-secondary">Search</button>
        </form>
        {places.isFetching && <Spinner className="mt-2" />}
        <div className="mt-2 space-y-1">
          {places.data?.map((p) => (
            <button
              key={`${p.osm_id}-${p.lat}`}
              onClick={() => {
                setPickedAddress(p.display_name);
                setPick({ place: p });
              }}
              className="flex w-full flex-col rounded-lg px-3 py-2 text-left hover:bg-slate-50"
            >
              <span className="text-sm font-medium">{p.name || p.display_name.split(",")[0]}</span>
              <span className="truncate text-xs text-slate-500">{p.display_name}</span>
            </button>
          ))}
          {places.data?.length === 0 && (
            <button className="btn-secondary w-full justify-start" disabled={lookup.isPending} onClick={() => lookup.mutate(geoQ)}>
              {lookup.isPending && <Spinner />} No exact match. Find the building at this address
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}
