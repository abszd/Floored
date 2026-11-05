import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { MapPin } from "lucide-react";
import { api } from "../lib/api";
import type { Building, Place } from "../lib/types";
import AddressInput from "./AddressInput";
import PlacePreviewMap from "./PlacePreviewMap";
import { Modal, Spinner } from "./ui";
import { useToast } from "./Toast";

/**
 * Rename a building or correct its address. A new address is looked up and
 * shown on the map next to the current outline; saving moves the building
 * there and re-places its floors on the new outline. The rooms found in the
 * plans are kept as they are.
 */
export default function EditBuildingDialog({ building, onClose }: { building: Building; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState(building.name);
  const [address, setAddress] = useState(building.address ?? "");
  const [moveTo, setMoveTo] = useState<Place | null>(null);
  const converted = building.floors.filter((f) => f.status !== "pending" && f.status !== "processing" && f.status !== "error").length;

  const find = useMutation({
    mutationFn: (picked?: Place) =>
      api.lookup(
        picked
          ? { name, address: picked.display_name, lat: picked.lat, lon: picked.lon, exclude_building_id: building.id }
          : { name, address, exclude_building_id: building.id },
      ),
    onSuccess: (r) => {
      if (r.building) toast.error("That's another building on your map", `${r.building.name} is already at that address.`);
      else setMoveTo(r.place);
    },
    onError: (e: Error) => toast.error("Couldn't find that address", e.message),
  });

  const save = useMutation({
    mutationFn: async () => {
      if (name.trim() && name.trim() !== building.name) await api.updateBuilding(building.id, { name: name.trim() });
      if (moveTo) return api.relocateBuilding(building.id, { place: moveTo, address: address.trim() || undefined });
      if (address.trim() !== (building.address ?? "")) return api.updateBuilding(building.id, { address: address.trim() });
      return building;
    },
    onSuccess: () => {
      qc.invalidateQueries();
      toast.success(moveTo ? `Moved ${name}${converted ? ` and re-placed ${converted} floor${converted === 1 ? "" : "s"}` : ""}` : `Saved ${name}`);
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't save", e.message),
  });

  return (
    <Modal title="Edit building" onClose={onClose}>
      <label className="label">Name</label>
      <input className="input" value={name} onChange={(e) => setName(e.target.value)} />

      <label className="label mt-4">Address</label>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (address.trim().length > 1) find.mutate(undefined);
        }}
      >
        <AddressInput
          value={address}
          placeholder="Start typing the street address"
          near={{ lat: building.lat, lon: building.lon }}
          onChange={(v) => {
            setAddress(v);
            setMoveTo(null);
          }}
          onPick={(p) => find.mutate(p)}
        />
        <button className="btn-secondary" disabled={address.trim().length < 2 || find.isPending}>
          {find.isPending ? <Spinner /> : <MapPin className="h-4 w-4" />} Find
        </button>
      </form>
      <p className="mt-1 text-xs text-slate-500">Pick a suggestion (or press Find) to move the building there. Saving without that only changes the text.</p>

      <div className="mt-3">
        <PlacePreviewMap place={moveTo} current={building} />
        <div className="mt-1.5 flex items-center gap-3 text-[11px] text-slate-500">
          <span className="inline-flex items-center gap-1">
            <span className="h-2.5 w-2.5 rounded-sm border border-dashed border-slate-500 bg-slate-300/50" /> Now
          </span>
          {moveTo && (
            <span className="inline-flex items-center gap-1">
              <span className="h-2.5 w-2.5 rounded-sm bg-indigo-500/60" /> {moveTo.display_name || "New location"}
            </span>
          )}
        </div>
        {moveTo && !moveTo.footprint && (
          <div className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
            OpenStreetMap has no outline for this building, so its floors will be placed at the address and need adjusting by hand.
          </div>
        )}
      </div>

      <div className="mt-5 flex justify-end gap-2">
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending && <Spinner />} {moveTo ? "Move building and re-place floors" : "Save"}
        </button>
      </div>
    </Modal>
  );
}
