import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Globe, History, Image as ImageIcon, Inbox, MapPin, Pencil, Play, Plus, RefreshCw, Trash2, Wand2 } from "lucide-react";
import { api, mediaUrl } from "../lib/api";
import { useJobs } from "../lib/jobs";
import { levelShort } from "../lib/placement";
import type { Candidate, Draft, Source } from "../lib/types";
import { CrawlCard } from "../components/ActivityPanel";
import DraftCard, { draftTarget } from "../components/DraftCard";
import { ConfidenceBar, Empty, Modal, Spinner } from "../components/ui";
import { useToast } from "../components/Toast";

const TABS = [
  { key: "review", label: "To review", status: "" },
  { key: "approved", label: "Processing", status: "approved" },
  { key: "done", label: "On the map", status: "processed" },
  { key: "rejected", label: "Rejected", status: "rejected" },
];

export default function DiscoverPage() {
  const [tab, setTab] = useState("review");
  const [params, setParams] = useSearchParams();
  const [addingSource, setAddingSourceState] = useState(params.get("add") === "1");
  const setAddingSource = (v: boolean) => {
    setAddingSourceState(v);
    if (!v && params.has("add")) setParams({}, { replace: true });
  };
  const [zoom, setZoom] = useState<Candidate | null>(null);
  const [thumb, setThumb] = useThumbSize();
  const { active } = useJobs();
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const status = TABS.find((t) => t.key === tab)!.status;
  const crawling = active.some((j) => j.kind === "crawl");
  const drafts = useQuery({
    queryKey: ["drafts"],
    queryFn: () => api.drafts(),
    enabled: tab === "review",
    refetchInterval: crawling || active.some((j) => j.kind === "match_candidates") ? 3000 : false,
  });
  const candidates = useQuery({
    queryKey: ["candidates", status],
    queryFn: () => api.candidates(status),
    enabled: tab !== "review",
    refetchInterval: tab === "approved" ? 2000 : false,
  });
  const reviewCount = drafts.data?.length;

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
                {t.key === "review" && reviewCount ? <span className="ml-1 text-xs text-slate-400">{reviewCount}</span> : null}
              </button>
            ))}
          </div>
          {tab === "review" && drafts.data && <ConfirmConfident drafts={drafts.data} />}
          <div className="flex-1" />
          <label className="flex items-center gap-2 text-xs text-slate-500" title="How big the floor plan pictures are">
            <ImageIcon className="h-3.5 w-3.5" />
            <input type="range" min={80} max={320} step={10} value={thumb} onChange={(e) => setThumb(Number(e.target.value))} className="w-28 accent-indigo-600" aria-label="Picture size" />
            <ImageIcon className="h-5 w-5" />
          </label>
        </div>

        {tab === "review" ? (
          <>
            {drafts.isLoading && <Spinner className="h-6 w-6 text-slate-400" />}
            {drafts.data?.length === 0 && (
              <div className="card">
                <Empty icon={<Inbox className="h-5 w-5" />} title={crawling ? "Looking for floor plans…" : "Nothing to review"}>
                  {crawling ? "Plans show up here, grouped by building, as the crawler finds them." : "Add a website and run a crawl to find floor plans."}
                </Empty>
              </div>
            )}
            {!!drafts.data?.length && (
              <p className="mb-3 text-xs text-slate-500">
                Plans are grouped into temp buildings by the name on the page. Check where each one is, fix the address if needed, and
                confirm to put all its floors on the map at once.
              </p>
            )}
            <div className="grid items-start gap-4" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${Math.max(340, thumb * 2.3 + 40)}px), 1fr))` }}>
              {drafts.data?.map((d) => (
                <DraftCard key={d.id} draft={d} others={drafts.data!.filter((o) => o.source_id === d.source_id)} thumb={thumb} onZoom={setZoom} />
              ))}
            </div>
          </>
        ) : (
          <>
            {candidates.isLoading && <Spinner className="h-6 w-6 text-slate-400" />}
            {candidates.data?.length === 0 && (
              <div className="card">
                <Empty icon={<Inbox className="h-5 w-5" />} title="Nothing here yet" />
              </div>
            )}
            <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${thumb + 100}px), 1fr))` }}>
              {candidates.data?.map((c) => <CandidateCard key={c.id} c={c} onZoom={() => setZoom(c)} />)}
            </div>
          </>
        )}
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
  const { track, active, setPanelOpen } = useJobs();
  const [editing, setEditing] = useState(false);
  const [rerun, setRerun] = useState(false);
  const running = active.find((j) => j.kind === "crawl" && j.payload.source_id === source.id);
  const crawl = useMutation({
    mutationFn: () => api.crawl(source.id),
    onSuccess: ({ job_id }) => track(job_id, `Crawl of ${source.name}`),
  });
  const queuedAt = active.filter((j) => j.status === "queued").findIndex((j) => j.id === running?.id);
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
        <button className="text-slate-300 hover:text-slate-600" onClick={() => setEditing(true)} title="Edit">
          <Pencil className="h-3.5 w-3.5" />
        </button>
        <button className="text-slate-300 hover:text-rose-500" onClick={() => confirm(`Remove ${source.name} and its findings?`) && remove.mutate()} title="Remove">
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
      {running ? (
        <div className="mt-3">
          {running.status === "queued" ? (
            <div className="text-[11px] text-slate-500">Waiting to start{queuedAt > 0 ? ` (${queuedAt} job${queuedAt > 1 ? "s" : ""} ahead)` : ""}…</div>
          ) : (
            <CrawlCard job={running} compact />
          )}
          <button className="mt-1 text-[11px] text-indigo-600 hover:underline" onClick={() => setPanelOpen(true)}>
            Show all activity
          </button>
        </div>
      ) : (
        <div className="mt-3 flex items-center justify-between gap-2">
          <div className="min-w-0 truncate text-xs text-slate-500">
            {source.last_crawled_at ? `${found} plans found` : "Not crawled yet"}
            {source.location_hint && <span> · {source.location_hint}</span>}
          </div>
          <div className="flex shrink-0 items-center gap-1 whitespace-nowrap">
            {(source.saved.responses > 0 || found > 0) && (
              <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setRerun(true)} title="Run it again from scratch, from saved copies of the site">
                <History className="h-3 w-3" /> Re-run
              </button>
            )}
            <button className="btn-secondary px-2 py-1 text-xs" onClick={() => crawl.mutate()} disabled={crawl.isPending}>
              {source.last_crawled_at ? <RefreshCw className="h-3 w-3" /> : <Play className="h-3 w-3" />}
              {source.last_crawled_at ? "Crawl again" : "Crawl"}
            </button>
          </div>
        </div>
      )}
      {editing && <AddSourceDialog source={source} onClose={() => setEditing(false)} />}
      {rerun && <RerunDialog source={source} onClose={() => setRerun(false)} />}
    </div>
  );
}

function megabytes(b: number) {
  return b < 1e6 ? `${Math.max(1, Math.round(b / 1e3))} KB` : `${(b / 1e6).toFixed(b < 1e8 ? 1 : 0)} MB`;
}

/** Run a website again from scratch, from saved copies, to test changes without hitting the site. */
function RerunDialog({ source, onClose }: { source: Source; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track, setPanelOpen } = useJobs();
  const hasSaved = source.saved.responses > 0;
  const [useSaved, setUseSaved] = useState(hasSaved);
  const [fresh, setFresh] = useState(true);
  const [offMap, setOffMap] = useState(false);
  const onMap = (source.candidate_counts.approved ?? 0) + (source.candidate_counts.processed ?? 0);
  const run = useMutation({
    mutationFn: () => api.crawl(source.id, { use_saved: useSaved, fresh, remove_from_map: fresh && offMap }),
    onSuccess: ({ job_id, reset }) => {
      track(job_id, `Re-run of ${source.name}`);
      qc.invalidateQueries();
      setPanelOpen(true);
      if (reset && (reset.plans || reset.floors))
        toast.success("Started over", `Cleared ${reset.plans} plan${reset.plans === 1 ? "" : "s"}${reset.floors ? ` and ${reset.floors} floor${reset.floors === 1 ? "" : "s"} on the map` : ""}.`);
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't start", e.message),
  });
  const clear = useMutation({
    mutationFn: () => api.clearSaved(source.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sources"] });
      setUseSaved(false);
      toast.success("Saved copies deleted", "The next crawl downloads everything again.");
    },
  });
  return (
    <Modal title={`Re-run ${source.name}`} onClose={onClose}>
      <div className="space-y-3 text-sm">
        <label className={`flex items-start gap-2 ${hasSaved ? "" : "opacity-50"}`}>
          <input type="checkbox" className="mt-1 accent-indigo-600" disabled={!hasSaved} checked={useSaved} onChange={(e) => setUseSaved(e.target.checked)} />
          <span>
            Use saved copies of the site
            <span className="block text-xs text-slate-500">
              {hasSaved
                ? `${source.saved.responses.toLocaleString()} pages and files (${megabytes(source.saved.bytes)}) from earlier crawls. Only anything missing is downloaded. Building lookups are saved too.`
                : "Nothing saved yet. This crawl saves a copy of everything it downloads, so the next re-run can use it."}
            </span>
          </span>
        </label>
        <label className="flex items-start gap-2">
          <input type="checkbox" className="mt-1 accent-indigo-600" checked={fresh} onChange={(e) => setFresh(e.target.checked)} />
          <span>
            Start over
            <span className="block text-xs text-slate-500">Forget the plans earlier crawls found and waiting for review, and find them all again.</span>
          </span>
        </label>
        {fresh && onMap > 0 && (
          <label className="ml-6 flex items-start gap-2">
            <input type="checkbox" className="mt-1 accent-indigo-600" checked={offMap} onChange={(e) => setOffMap(e.target.checked)} />
            <span>
              Also take its {onMap} plan{onMap === 1 ? "" : "s"} off the map
              <span className="block text-xs text-slate-500">So matching and conversion run again for them too. Room fixes on those floors are lost.</span>
            </span>
          </label>
        )}
        <div className="flex items-center gap-2 pt-2">
          {hasSaved && (
            <button className="btn-ghost px-2 py-1 text-xs text-slate-500" disabled={clear.isPending} onClick={() => confirm("Delete the saved copies of this website?") && clear.mutate()}>
              Delete saved copies
            </button>
          )}
          <div className="flex-1" />
          <button className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-primary" disabled={run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? <Spinner /> : <History className="h-4 w-4" />} Re-run
          </button>
        </div>
      </div>
    </Modal>
  );
}

function AddSourceDialog({ onClose, source }: { onClose: () => void; source?: Source }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track, setPanelOpen } = useJobs();
  const [urls, setUrls] = useState(source ? source.seed_urls.join("\n") : "");
  const [name, setName] = useState(source?.name ?? "");
  const [hint, setHint] = useState(source?.location_hint ?? "");
  const [maxPages, setMaxPages] = useState(source?.max_pages ?? 200);
  const list = urls.split(/\s+/).filter((u) => /^https?:\/\//.test(u));

  const save = useMutation({
    mutationFn: () =>
      api.updateSource(source!.id, {
        name: name.trim() || source!.name,
        seed_urls: list,
        location_hint: hint.trim() || null,
        max_pages: maxPages,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sources"] });
      qc.invalidateQueries({ queryKey: ["drafts"] });
      toast.success("Website updated", "Changes apply to the next crawl. Use “Look up the address again” on a temp building to retry it with a new location.");
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't save", e.message),
  });

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
      setPanelOpen(true);
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't add website", e.message),
  });

  return (
    <Modal title={source ? `Edit ${source.name}` : "Search a website for floor plans"} onClose={onClose}>
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
        {source ? (
          <button className="btn-primary" disabled={!list.length || save.isPending} onClick={() => save.mutate()}>
            {save.isPending && <Spinner />} Save
          </button>
        ) : (
          <button className="btn-primary" disabled={!list.length || create.isPending} onClick={() => create.mutate()}>
            {create.isPending && <Spinner />} Add and crawl
          </button>
        )}
      </div>
    </Modal>
  );
}

const THUMB_KEY = "floored.reviewThumb";

/** Size of the plan pictures, remembered in this browser. */
function useThumbSize(): [number, (n: number) => void] {
  const [size, setSize] = useState(() => {
    try {
      return Number(localStorage.getItem(THUMB_KEY)) || 140;
    } catch {
      return 140;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(THUMB_KEY, String(size));
    } catch {
      /* private mode */
    }
  }, [size]);
  return [size, setSize];
}

/** Confirm every temp building whose location we're fairly sure of, in one go. */
function ConfirmConfident({ drafts }: { drafts: Draft[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { track } = useJobs();
  const ready = drafts.filter((d) => draftTarget(d) && (d.building || d.suggestion?.chosen || (d.suggestion?.confidence ?? 0) >= 0.7));
  const [busy, setBusy] = useState(false);
  if (!ready.length) return null;
  return (
    <button
      className="btn-secondary text-xs"
      disabled={busy}
      onClick={async () => {
        if (!confirm(`Put ${ready.length} building${ready.length > 1 ? "s" : ""} on the map with all their floor plans?`)) return;
        setBusy(true);
        let floors = 0;
        for (const d of ready) {
          try {
            const r = await api.confirmDraft(d.id);
            r.job_ids.forEach((id) => track(id, `Converting ${r.building.name}`, { quiet: true }));
            floors += r.job_ids.length;
          } catch (e) {
            toast.error(`Couldn't add ${d.name}`, (e as Error).message);
          }
        }
        setBusy(false);
        qc.invalidateQueries();
        toast.success(`Added ${ready.length} buildings`, `Finding rooms in ${floors} floor plans. Follow along in Activity.`);
      }}
    >
      {busy ? <Spinner /> : <Wand2 className="h-3.5 w-3.5" />} Confirm {ready.length} good match{ready.length > 1 ? "es" : ""}
    </button>
  );
}

function CandidateCard({ c, onZoom }: { c: Candidate; onZoom: () => void }) {
  return (
    <div className="card flex flex-col overflow-hidden">
      <button onClick={onZoom} className="relative block aspect-[4/3] bg-slate-50">
        {c.image_url && <img src={mediaUrl(c.image_url)} loading="lazy" className="h-full w-full object-contain p-2" />}
        {c.level_guess != null && (
          <span className="absolute left-2 top-2 rounded-md bg-slate-900/80 px-1.5 py-0.5 text-[11px] font-semibold text-white">Level {levelShort(c.level_guess)}</span>
        )}
      </button>
      <div className="flex flex-1 flex-col gap-1 p-3">
        <div className="truncate text-sm font-medium" title={c.building_guess ?? ""}>
          {c.building_name ?? c.building_guess ?? "Unnamed"} {c.level_label && <span className="text-slate-400">· {c.level_label}</span>}
        </div>
        {c.building_id && c.match_confidence > 0 && <ConfidenceBar value={c.match_confidence} />}
        {c.error && <div className="line-clamp-2 text-[11px] text-rose-600">{c.error}</div>}
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
      </div>
    </div>
  );
}
