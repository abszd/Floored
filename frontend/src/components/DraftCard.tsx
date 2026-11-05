import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Building2, Check, ExternalLink, MoreHorizontal, RefreshCw, Search, X } from "lucide-react";
import clsx from "clsx";
import { api, mediaUrl } from "../lib/api";
import { useJobs } from "../lib/jobs";
import { levelShort } from "../lib/placement";
import type { Candidate, Draft } from "../lib/types";
import AddressInput from "./AddressInput";
import ChooseBuildingDialog, { type Pick } from "./ChooseBuildingDialog";
import PlacePreviewMap from "./PlacePreviewMap";
import { ConfidenceBar, Spinner } from "./ui";
import { useToast } from "./Toast";

/** Mounts its children only while near the screen: a page of maps would run out of WebGL contexts. */
function WhenVisible({ children, className }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setVisible(e.isIntersecting), { rootMargin: "300px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return (
    <div ref={ref} className={className}>
      {visible ? children : <div className="h-full w-full rounded-lg border border-slate-200 bg-slate-50" />}
    </div>
  );
}

/** The building the plans will go to once confirmed, if we know one. */
export function draftTarget(d: Draft): { label: string; detail: string; existing: boolean; spot: { lat: number; lon: number; footprint?: unknown } } | null {
  if (d.building) return { label: d.building.name, detail: d.building.address ?? "", existing: true, spot: d.building };
  const s = d.suggestion;
  if (s?.building) return { label: s.building.name, detail: s.building.address ?? "", existing: true, spot: s.building };
  if (s?.place) return { label: s.place.name || s.place.display_name.split(",")[0], detail: s.place.display_name, existing: false, spot: s.place };
  return null;
}

/**
 * A temp building: plans the crawler found that seem to show one building.
 * The user says where it is once, and every plan in it goes on the map.
 */
export default function DraftCard({
  draft,
  others,
  thumb,
  onZoom,
}: {
  draft: Draft;
  /** Other temp buildings from the same site, to move plans to. */
  others: Draft[];
  thumb: number;
  onZoom: (c: Candidate) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track } = useJobs();
  const [name, setName] = useState(draft.name);
  const [address, setAddress] = useState(draft.address ?? "");
  const [choosing, setChoosing] = useState(false);
  const [menu, setMenu] = useState(false);
  useEffect(() => setName(draft.name), [draft.name]);
  useEffect(() => setAddress(draft.address ?? ""), [draft.address]);

  const refresh = () => qc.invalidateQueries({ queryKey: ["drafts"] });
  const update = useMutation({
    mutationFn: (body: Parameters<typeof api.updateDraft>[1]) => api.updateDraft(draft.id, body),
    onSuccess: refresh,
    onError: (e: Error) => toast.error("Couldn't save", e.message),
  });
  const choose = async (pick: Pick, addr?: string) => {
    await api.updateDraft(draft.id, {
      address: addr ?? (address || undefined),
      choice: pick.building ? { building_id: pick.building.id } : { place: pick.place },
    });
    refresh();
  };
  const lookup = useMutation({
    mutationFn: (target: { address: string; lat?: number; lon?: number }) =>
      api.lookup({ name: name || draft.name, address: target.address, hint: draft.location_hint, lat: target.lat, lon: target.lon }),
    onSuccess: (r, target) => {
      if (r.building) return choose({ building: r.building }, target.address);
      if (r.place) return choose({ place: r.place }, target.address);
    },
    onError: (e: Error) => toast.error("Couldn't find that address", e.message),
  });
  const add = useMutation({
    mutationFn: () => api.confirmDraft(draft.id, { name: name.trim() || undefined }),
    onSuccess: ({ building, job_ids }) => {
      job_ids.forEach((id) => track(id, `Converting ${building.name}`, { quiet: true }));
      toast.success(
        `${building.name} is on the map`,
        job_ids.length ? `Finding rooms in ${job_ids.length} floor plan${job_ids.length > 1 ? "s" : ""}. Follow along in Activity.` : undefined,
      );
      qc.invalidateQueries();
    },
    onError: (e: Error) => toast.error("Couldn't add the building", e.message),
  });
  const rejectAll = useMutation({ mutationFn: () => api.rejectDraft(draft.id), onSuccess: () => qc.invalidateQueries() });
  const rematch = useMutation({
    mutationFn: () => api.rematchDraft(draft.id),
    onSuccess: ({ job_id }) => {
      track(job_id, `Finding ${draft.name}`, { quiet: true });
      refresh();
    },
  });

  const target = draftTarget(draft);
  // Levels that two plans claim (or that the building already has): they'd overlap, so one will be moved.
  const existingBuilding = draft.building ?? draft.suggestion?.building ?? null;
  const onMap = new Set((existingBuilding?.floors ?? []).map((f) => f.level));
  const counts = new Map<number, number>();
  draft.candidates.forEach((c) => c.level_guess != null && counts.set(c.level_guess, (counts.get(c.level_guess) ?? 0) + 1));
  const clash = (c: Candidate) =>
    c.level_guess == null ? null : (counts.get(c.level_guess) ?? 0) > 1 ? "Another plan here says the same level" : onMap.has(c.level_guess) ? `${existingBuilding!.name} already has this level` : null;
  const s = draft.suggestion;
  const n = draft.candidates.length;
  const looking = draft.match_state === "pending" || rematch.isPending;

  return (
    <div className="card flex flex-col">
      {/* Header: the temp building's name, editable */}
      <div className="flex items-start gap-2 border-b border-slate-100 p-3">
        <Building2 className="mt-1.5 h-4 w-4 shrink-0 text-amber-500" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <input
              className="-ml-1 min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-1 py-0.5 text-sm font-semibold hover:border-slate-200 focus:border-indigo-300 focus:outline-none"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onBlur={() => name.trim() && name.trim() !== draft.name && update.mutate({ name: name.trim() })}
              onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
              title="Building name (click to edit)"
            />
            {!draft.building && <span className="shrink-0 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 ring-1 ring-amber-200">Temp building</span>}
          </div>
          <div className="truncate text-[11px] text-slate-500">
            {n} plan{n === 1 ? "" : "s"}
            {draft.source_name && ` from ${draft.source_name}`}
            {draft.page_urls[0] && (
              <a href={draft.page_urls[0]} target="_blank" rel="noreferrer" className="ml-1.5 inline-flex items-center gap-0.5 text-slate-400 hover:text-slate-600">
                <ExternalLink className="h-3 w-3" /> page
              </a>
            )}
          </div>
        </div>
        <div className="relative">
          <button className="btn-ghost p-1 text-slate-400" onClick={() => setMenu((m) => !m)} aria-label="More">
            <MoreHorizontal className="h-4 w-4" />
          </button>
          {menu && (
            <div className="panel absolute right-0 top-full z-20 mt-1 w-56 p-1 text-sm" onMouseLeave={() => setMenu(false)}>
              <button className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left hover:bg-slate-50" onClick={() => (setMenu(false), rematch.mutate())}>
                <RefreshCw className="h-4 w-4" /> Look up the address again
              </button>
              <button
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-rose-600 hover:bg-rose-50"
                onClick={() => (setMenu(false), window.confirm(`Reject all ${n} plans in ${draft.name}?`) && rejectAll.mutate())}
              >
                <X className="h-4 w-4" /> Not floor plans, reject all
              </button>
            </div>
          )}
        </div>
      </div>

      {/* The plans in it */}
      <div className="flex flex-wrap gap-2 p-3">
        {draft.candidates.map((c) => (
          <PlanTile key={c.id} c={c} draft={draft} others={others} size={thumb} clash={clash(c)} onZoom={() => onZoom(c)} />
        ))}
      </div>

      {/* Where it is */}
      <div className="mt-auto space-y-2 border-t border-slate-100 p-3">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Where is it?</div>
        {target && (
          <WhenVisible className="h-36">
            <PlacePreviewMap place={target.spot as never} className="h-36" />
          </WhenVisible>
        )}
        {target ? (
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-medium">
                {target.label}
                {target.existing && <span className="ml-1 text-xs font-normal text-emerald-600">· already on your map</span>}
              </div>
              <div className="line-clamp-2 text-[11px] text-slate-500">{target.detail}</div>
              {s && !draft.building && <div className="text-[11px] text-slate-400">{s.chosen ? "You picked this" : s.reason}</div>}
            </div>
            {s && !s.chosen && !draft.building && <ConfidenceBar value={s.confidence} />}
          </div>
        ) : looking ? (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <Spinner /> Looking up the address…
          </div>
        ) : (
          <div className="text-sm text-amber-700">Couldn't find it automatically. Type its address below.</div>
        )}
        <form
          className="flex gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (address.trim()) lookup.mutate({ address: address.trim() });
          }}
        >
          <AddressInput
            value={address}
            onChange={setAddress}
            onPick={(p) => lookup.mutate({ address: p.display_name, lat: p.lat, lon: p.lon })}
            near={{ hint: draft.location_hint }}
            placeholder={target ? "Wrong place? Type the address" : "Type the address"}
            className="py-1.5 text-sm"
          />
          <button className="btn-secondary px-2 py-1 text-xs" disabled={lookup.isPending || !address.trim()} title="Find this address">
            {lookup.isPending ? <Spinner /> : <Search className="h-3.5 w-3.5" />}
          </button>
        </form>
        <div className="flex items-center gap-2 pt-1">
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setChoosing(true)}>
            Pick from my buildings
          </button>
          <div className="flex-1" />
          <button className="btn-primary px-3 py-1.5 text-xs" disabled={!target || add.isPending || n === 0} onClick={() => add.mutate()}>
            {add.isPending ? <Spinner /> : <Check className="h-3.5 w-3.5" />}
            {target?.existing ? `Add ${n} floor${n === 1 ? "" : "s"} to ${target.label}` : `Create building & add ${n} floor${n === 1 ? "" : "s"}`}
          </button>
        </div>
        {draft.building && (
          <Link to={`/?b=${draft.building.id}`} className="block text-[11px] text-indigo-600 hover:underline">
            View {draft.building.name} on the map
          </Link>
        )}
      </div>

      {choosing && (
        <ChooseBuildingDialog name={name} address={draft.address} hint={draft.location_hint} onPicked={choose} onClose={() => setChoosing(false)} />
      )}
    </div>
  );
}

function PlanTile({
  c,
  draft,
  others,
  size,
  clash,
  onZoom,
}: {
  c: Candidate;
  draft: Draft;
  others: Draft[];
  size: number;
  /** Why this plan's level would overlap another's, if it would. */
  clash: string | null;
  onZoom: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [level, setLevel] = useState(c.level_guess != null ? String(c.level_guess) : "");
  useEffect(() => setLevel(c.level_guess != null ? String(c.level_guess) : ""), [c.level_guess]);
  const patch = useMutation({
    mutationFn: (body: Parameters<typeof api.updateCandidate>[1]) => api.updateCandidate(c.id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["drafts"] }),
    onError: (e: Error) => toast.error("Couldn't save", e.message),
  });
  const reject = useMutation({ mutationFn: () => api.reject(c.id), onSuccess: () => qc.invalidateQueries({ queryKey: ["drafts"] }) });
  const saveLevel = () => {
    const v = level.trim() === "" ? null : Number(level);
    if (v !== null && Number.isNaN(v)) return setLevel(c.level_guess != null ? String(c.level_guess) : "");
    if (v !== c.level_guess) patch.mutate({ level_guess: v });
  };

  return (
    <div className="group relative flex flex-col overflow-hidden rounded-lg border border-slate-200 bg-white" style={{ width: size }}>
      <button onClick={onZoom} className="block bg-slate-50" style={{ height: size * 0.75 }} title={c.context ?? undefined}>
        {c.image_url && <img src={mediaUrl(c.image_url)} loading="lazy" className="h-full w-full object-contain p-1" />}
      </button>
      <button
        className="absolute right-1 top-1 rounded-md bg-white/90 p-0.5 text-slate-400 opacity-0 shadow-sm transition hover:text-rose-600 group-hover:opacity-100"
        title="Not a floor plan"
        onClick={() => reject.mutate()}
      >
        <X className="h-3.5 w-3.5" />
      </button>
      <div className="flex items-center gap-1 border-t border-slate-100 p-1">
        <span className="text-[10px] text-slate-400">Lvl</span>
        <input
          className={clsx("w-9 rounded border border-slate-200 px-1 py-0.5 text-center text-[11px]", (level === "" || clash) && "border-amber-400 bg-amber-50")}
          value={level}
          placeholder="?"
          title={
            clash
              ? `${clash}. Plans never share a level, so one would be moved up; set the right level.`
              : level === ""
                ? "The page didn't say which floor this is (0 = ground, -1 = basement)"
                : (c.level_label ?? "Which floor this is (0 = ground, -1 = basement)")
          }
          onChange={(e) => setLevel(e.target.value)}
          onBlur={saveLevel}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        {size >= 130 && c.level_guess != null && <span className="truncate text-[10px] text-slate-500">{c.level_label ?? `Level ${levelShort(c.level_guess)}`}</span>}
        <div className="flex-1" />
        <select
          className="w-5 shrink-0 cursor-pointer appearance-none rounded bg-transparent text-center text-[11px] text-slate-400 hover:bg-slate-100"
          value=""
          title="Move to another building"
          onChange={(e) => e.target.value && patch.mutate({ draft_id: e.target.value })}
        >
          <option value="">⋯</option>
          <option value="new">Move to its own building</option>
          {others
            .filter((o) => o.id !== draft.id)
            .map((o) => (
              <option key={o.id} value={o.id}>
                Move to {o.name}
              </option>
            ))}
        </select>
      </div>
    </div>
  );
}
