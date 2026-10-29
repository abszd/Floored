import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ExternalLink, Globe, Play, Plus, RefreshCw, Search, Trash2, X, Inbox, Wand2, MapPin } from "lucide-react";
import { api, mediaUrl } from "../lib/api";
import { useJobs } from "../lib/jobs";
import { levelShort } from "../lib/placement";
import type { Candidate, Source } from "../lib/types";
import { ConfidenceBar, Empty, Modal, Spinner } from "../components/ui";
import { useToast } from "../components/Toast";

const TABS = [
  { key: "review", label: "To review", status: "matched,unmatched,new,error" },
  { key: "approved", label: "Processing", status: "approved" },
  { key: "done", label: "On the map", status: "processed" },
  { key: "rejected", label: "Rejected", status: "rejected" },
];

export default function DiscoverPage() {
  const [tab, setTab] = useState("review");
  const [addingSource, setAddingSource] = useState(false);
  const [zoom, setZoom] = useState<Candidate | null>(null);
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const status = TABS.find((t) => t.key === tab)!.status;
  const candidates = useQuery({
    queryKey: ["candidates", status],
    queryFn: () => api.candidates(status),
    refetchInterval: tab === "approved" ? 2000 : false,
  });

  return (
    <div className="mx-auto flex h-full max-w-7xl flex-col gap-6 overflow-y-auto p-6 lg:flex-row">
      {/* Sources */}
      <section className="w-full shrink-0 lg:w-80">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Websites to search</h2>
          <button className="btn-primary px-2.5 py-1.5 text-xs" onClick={() => setAddingSource(true)}>
            <Plus className="h-3.5 w-3.5" /> Add
          </button>
        </div>
        <p className="mb-4 text-xs text-slate-500">
          Floored crawls these sites for floor plan images and PDFs, works out which building each one belongs to, and puts the
          confident matches straight on the map.
        </p>
        <div className="space-y-2">
          {sources.data?.length === 0 && (
            <div className="card p-4 text-sm text-slate-500">
              Start with a page that links to floor plans, like a university facilities or campus map site.
            </div>
          )}
          {sources.data?.map((s) => <SourceCard key={s.id} source={s} />)}
        </div>
      </section>

      {/* Review queue */}
      <section className="min-w-0 flex-1">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg bg-slate-100 p-1 text-sm">
            {TABS.map((t) => (
              <button key={t.key} onClick={() => setTab(t.key)} className={`rounded-md px-3 py-1 ${tab === t.key ? "bg-white font-medium shadow-sm" : "text-slate-500"}`}>
                {t.label}
              </button>
            ))}
          </div>
          {tab === "review" && candidates.data && candidates.data.length > 0 && <BulkApprove candidates={candidates.data} />}
        </div>

        {candidates.isLoading && <Spinner className="h-6 w-6 text-slate-400" />}
        {candidates.data?.length === 0 && (
          <div className="card">
            <Empty icon={<Inbox className="h-5 w-5" />} title={tab === "review" ? "Nothing to review" : "Nothing here yet"}>
              {tab === "review" ? "Add a website and run a crawl to find floor plans." : null}
            </Empty>
          </div>
        )}
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {candidates.data?.map((c) => <CandidateCard key={c.id} c={c} onZoom={() => setZoom(c)} />)}
        </div>
      </section>

      {addingSource && <AddSourceDialog onClose={() => setAddingSource(false)} />}
      {zoom && (
        <Modal title={zoom.building_guess ?? "Floor plan"} onClose={() => setZoom(null)} wide>
          <img src={mediaUrl(zoom.image_url)} className="max-h-[70vh] w-full object-contain" />
          {zoom.context && <p className="mt-3 text-xs text-slate-500">{zoom.context}</p>}
        </Modal>
      )}
    </div>
  );
}

function SourceCard({ source }: { source: Source }) {
  const qc = useQueryClient();
  const { track, active } = useJobs();
  const running = active.find((j) => j.kind === "crawl" && j.payload.source_id === source.id);
  const crawl = useMutation({
    mutationFn: () => api.crawl(source.id),
    onSuccess: ({ job_id }) => track(job_id, `Crawl of ${source.name}`),
  });
  const remove = useMutation({ mutationFn: () => api.deleteSource(source.id), onSuccess: () => qc.invalidateQueries() });
  const counts = source.candidate_counts;
  const found = Object.values(counts).reduce((a, b) => a + b, 0);

  return (
    <div className="card p-3">
      <div className="flex items-start gap-2">
        <Globe className="mt-0.5 h-4 w-4 shrink-0 text-indigo-500" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{source.name}</div>
          <div className="truncate text-xs text-slate-500">{source.seed_urls[0]}</div>
        </div>
        <button className="text-slate-300 hover:text-rose-500" onClick={() => confirm(`Remove ${source.name} and its findings?`) && remove.mutate()}>
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
      {running ? (
        <div className="mt-3">
          <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${Math.max(4, running.progress * 100)}%` }} />
          </div>
          <div className="mt-1 truncate text-[11px] text-slate-500">{running.message ?? "Starting…"}</div>
        </div>
      ) : (
        <div className="mt-3 flex items-center justify-between">
          <div className="text-xs text-slate-500">
            {source.last_crawled_at ? `${found} plans found` : "Not crawled yet"}
            {source.location_hint && <span> · {source.location_hint}</span>}
          </div>
          <button className="btn-secondary px-2 py-1 text-xs" onClick={() => crawl.mutate()} disabled={crawl.isPending}>
            {source.last_crawled_at ? <RefreshCw className="h-3 w-3" /> : <Play className="h-3 w-3" />}
            {source.last_crawled_at ? "Crawl again" : "Crawl"}
          </button>
        </div>
      )}
    </div>
  );
}

function AddSourceDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track } = useJobs();
  const [urls, setUrls] = useState("");
  const [name, setName] = useState("");
  const [hint, setHint] = useState("");
  const [maxPages, setMaxPages] = useState(200);
  const list = urls.split(/\s+/).filter((u) => /^https?:\/\//.test(u));

  const create = useMutation({
    mutationFn: async () => {
      const src = await api.createSource({
        name: name.trim() || new URL(list[0]).hostname,
        seed_urls: list,
        allowed_domains: [],
        location_hint: hint.trim() || null,
        max_pages: maxPages,
        max_depth: 3,
      });
      const { job_id } = await api.crawl(src.id);
      track(job_id, `Crawl of ${src.name}`);
      return src;
    },
    onSuccess: () => {
      qc.invalidateQueries();
      toast.success("Crawl started", "Found plans will show up in the review list.");
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't add website", e.message),
  });

  return (
    <Modal title="Search a website for floor plans" onClose={onClose}>
      <div className="space-y-4">
        <div>
          <label className="label">Start page(s)</label>
          <textarea className="input h-20 resize-none" placeholder="https://facilities.example.edu/floor-plans" value={urls} onChange={(e) => setUrls(e.target.value)} />
          <p className="mt-1 text-xs text-slate-500">The crawler stays on these sites and follows links that look like they lead to plans.</p>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label">Name</label>
            <input className="input" placeholder="UW–Madison facilities" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <label className="label">Where are the buildings?</label>
            <input className="input" placeholder="Madison, WI" value={hint} onChange={(e) => setHint(e.target.value)} />
          </div>
        </div>
        <p className="-mt-2 text-xs text-slate-500">The location helps match names like “Union South” to the right building.</p>
        <div>
          <label className="label">Pages to read at most</label>
          <input type="number" className="input w-32" min={1} max={5000} value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))} />
        </div>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="btn-primary" disabled={!list.length || create.isPending} onClick={() => create.mutate()}>
          {create.isPending && <Spinner />} Add and crawl
        </button>
      </div>
    </Modal>
  );
}

function BulkApprove({ candidates }: { candidates: Candidate[] }) {
  const { track } = useJobs();
  const ready = candidates.filter((c) => c.status === "matched" && c.match_confidence >= 0.7);
  const [busy, setBusy] = useState(false);
  if (!ready.length) return null;
  return (
    <button
      className="btn-secondary text-xs"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        for (const c of ready) {
          const { job_id } = await api.approve(c.id);
          track(job_id, `${c.building_name ?? "Plan"} ${c.level_label ?? ""}`.trim());
        }
        setBusy(false);
      }}
    >
      <Wand2 className="h-3.5 w-3.5" /> Approve {ready.length} good match{ready.length > 1 ? "es" : ""}
    </button>
  );
}

function CandidateCard({ c, onZoom }: { c: Candidate; onZoom: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track } = useJobs();
  const [picking, setPicking] = useState(false);
  const [level, setLevel] = useState(c.level_guess != null ? String(c.level_guess) : "");

  const approve = useMutation({
    mutationFn: () => api.approve(c.id, { level: level === "" ? undefined : Number(level) }),
    onSuccess: ({ job_id }) => {
      track(job_id, `${c.building_name ?? "Plan"} ${c.level_label ?? ""}`.trim());
      qc.invalidateQueries({ queryKey: ["candidates"] });
    },
    onError: (e: Error) => toast.error("Couldn't approve", e.message),
  });
  const reject = useMutation({ mutationFn: () => api.reject(c.id), onSuccess: () => qc.invalidateQueries({ queryKey: ["candidates"] }) });
  const reviewable = ["matched", "unmatched", "new", "error"].includes(c.status);

  return (
    <div className="card flex flex-col overflow-hidden">
      <button onClick={onZoom} className="relative block aspect-[4/3] bg-slate-50">
        {c.image_url && <img src={mediaUrl(c.image_url)} loading="lazy" className="h-full w-full object-contain p-2" />}
        {c.level_guess != null && (
          <span className="absolute left-2 top-2 rounded-md bg-slate-900/80 px-1.5 py-0.5 text-[11px] font-semibold text-white">Level {levelShort(c.level_guess)}</span>
        )}
      </button>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <div>
          <div className="text-[11px] uppercase tracking-wide text-slate-400">Found as</div>
          <div className="truncate text-sm font-medium" title={c.building_guess ?? ""}>
            {c.building_guess ?? "Unnamed"} {c.level_label && <span className="text-slate-400">· {c.level_label}</span>}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wide text-slate-400">Matched to</div>
          {c.building_id ? (
            <div className="flex items-center justify-between gap-2">
              <Link to={`/?b=${c.building_id}`} className="truncate text-sm font-medium text-indigo-600 hover:underline">
                {c.building_name}
              </Link>
              <ConfidenceBar value={c.match_confidence} />
            </div>
          ) : (
            <div className="text-sm text-amber-600">No building found</div>
          )}
          {c.match_reason && <div className="mt-0.5 line-clamp-2 text-[11px] text-slate-400">{c.match_reason}</div>}
          {c.error && <div className="mt-0.5 line-clamp-2 text-[11px] text-rose-600">{c.error}</div>}
        </div>
        <div className="mt-auto flex items-center gap-2 pt-1 text-xs">
          {c.page_url && (
            <a href={c.page_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-slate-400 hover:text-slate-600">
              <ExternalLink className="h-3 w-3" /> Source
            </a>
          )}
          {c.floor_id && c.building_id && (
            <Link to={`/?b=${c.building_id}`} className="inline-flex items-center gap-1 text-indigo-600 hover:underline">
              <MapPin className="h-3 w-3" /> View on map
            </Link>
          )}
        </div>
        {reviewable && (
          <div className="flex items-center gap-1.5 border-t border-slate-100 pt-2">
            <input className="input w-14 px-2 py-1 text-xs" placeholder="Lvl" title="Level" value={level} onChange={(e) => setLevel(e.target.value)} />
            <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setPicking(true)}>
              <Search className="h-3.5 w-3.5" /> {c.building_id ? "Change" : "Choose"}
            </button>
            <div className="flex-1" />
            <button className="btn-ghost px-2 py-1 text-xs text-slate-500" onClick={() => reject.mutate()} title="Not a floor plan, or not wanted">
              <X className="h-3.5 w-3.5" />
            </button>
            <button className="btn-primary px-2.5 py-1 text-xs" disabled={!c.building_id || approve.isPending} onClick={() => approve.mutate()}>
              <Check className="h-3.5 w-3.5" /> Approve
            </button>
          </div>
        )}
      </div>
      {picking && <ChooseBuildingDialog candidate={c} onClose={() => setPicking(false)} />}
    </div>
  );
}

function ChooseBuildingDialog({ candidate, onClose }: { candidate: Candidate; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState(candidate.building_guess ?? "");
  const [geoQ, setGeoQ] = useState("");
  const buildings = useQuery({ queryKey: ["buildings"], queryFn: api.buildings });
  const places = useQuery({ queryKey: ["geocode", geoQ], queryFn: () => api.geocode(geoQ), enabled: geoQ.length > 1 });
  const filtered = useMemo(
    () => (buildings.data ?? []).filter((b) => !q || `${b.name} ${b.address ?? ""} ${b.aliases.join(" ")}`.toLowerCase().includes(q.toLowerCase())),
    [buildings.data, q],
  );

  const choose = useMutation({
    mutationFn: async (target: { buildingId?: string; place?: Parameters<typeof api.createBuilding>[0]["place"] }) => {
      let id = target.buildingId;
      if (!id) id = (await api.createBuilding({ place: target.place })).id;
      return api.updateCandidate(candidate.id, { building_id: id });
    },
    onSuccess: () => {
      qc.invalidateQueries();
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't set building", e.message),
  });

  return (
    <Modal title="Which building is this?" onClose={onClose}>
      <input autoFocus className="input" placeholder="Filter your buildings" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="mt-2 max-h-56 space-y-1 overflow-auto">
        {filtered.map((b) => (
          <button key={b.id} onClick={() => choose.mutate({ buildingId: b.id })} className="flex w-full flex-col rounded-lg px-3 py-2 text-left hover:bg-slate-50">
            <span className="text-sm font-medium">{b.name}</span>
            <span className="truncate text-xs text-slate-500">{b.address}</span>
          </button>
        ))}
        {filtered.length === 0 && <div className="px-3 py-2 text-sm text-slate-500">None of your buildings match.</div>}
      </div>
      <div className="mt-4 border-t border-slate-100 pt-4">
        <label className="label">Or find it on OpenStreetMap</label>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setGeoQ(q.trim());
          }}
        >
          <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Building name and city, or address" />
          <button className="btn-secondary">Search</button>
        </form>
        {places.isFetching && <Spinner className="mt-2" />}
        <div className="mt-2 space-y-1">
          {places.data?.map((p) => (
            <button key={`${p.osm_id}-${p.lat}`} onClick={() => choose.mutate({ place: p })} className="flex w-full flex-col rounded-lg px-3 py-2 text-left hover:bg-slate-50">
              <span className="text-sm font-medium">{p.name || p.display_name.split(",")[0]}</span>
              <span className="truncate text-xs text-slate-500">{p.display_name}</span>
            </button>
          ))}
        </div>
      </div>
    </Modal>
  );
}
