import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { Activity, Job } from "./types";
import { useToast } from "../components/Toast";

interface TrackOptions {
  onDone?: (job: Job) => void;
  /** Don't toast when it finishes (one of many jobs started together). */
  quiet?: boolean;
}

interface JobsContext {
  track: (jobId: string, label: string, opts?: TrackOptions | ((job: Job) => void)) => void;
  /** Running and queued jobs. */
  active: Job[];
  activity: Activity | undefined;
  panelOpen: boolean;
  setPanelOpen: (open: boolean) => void;
}

const Ctx = createContext<JobsContext | null>(null);

/** What a finished crawl found, in words. */
export function crawlSummary(job: Job): string {
  const r = job.result as {
    found?: number;
    buildings?: number;
    auto_buildings?: number;
    to_review?: number;
    stopped?: boolean;
    from_saved?: number;
    downloaded?: number;
  };
  if (r.found == null) return job.message ?? "";
  if (!r.found) return r.stopped ? "Stopped early. No floor plans found yet." : "No new floor plans found.";
  const parts = [`${r.found} plan${r.found === 1 ? "" : "s"} in ${r.buildings} building${r.buildings === 1 ? "" : "s"}`];
  if (r.auto_buildings) parts.push(`${r.auto_buildings} put on the map`);
  if (r.to_review) parts.push(`${r.to_review} to review`);
  const offline = r.from_saved ? ` Used ${r.from_saved} saved page${r.from_saved === 1 ? "" : "s"} and files${r.downloaded ? `, downloaded ${r.downloaded}` : ""}.` : "";
  return (r.stopped ? "Stopped early. " : "") + parts.join(", ") + "." + offline;
}

/** Polls the background workers, refreshes data as jobs finish, and reports outcomes. */
export function JobsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [tracked, setTracked] = useState<Record<string, { label: string; quiet?: boolean }>>({});
  const [panelOpen, setPanelOpen] = useState(false);
  const callbacks = useRef<Record<string, (job: Job) => void>>({});
  const seenDone = useRef<Set<string> | null>(null);

  const { data: activity } = useQuery({
    queryKey: ["activity"],
    queryFn: api.activity,
    refetchInterval: (q) => {
      const a = q.state.data;
      const busy = (a?.running.length ?? 0) + (a?.queued.length ?? 0) > 0 || Object.keys(tracked).length > 0;
      return busy ? 1500 : panelOpen ? 5000 : 15000;
    },
  });
  const active = useMemo(() => [...(activity?.running ?? []), ...(activity?.queued ?? [])], [activity]);

  // While a crawl runs, its plans and temp buildings appear as they're found.
  const crawling = active.some((j) => j.kind === "crawl" && j.status === "running");
  const plansFound = active.reduce((n, j) => n + (j.detail.plans_found ?? 0), 0);
  useEffect(() => {
    if (crawling) {
      qc.invalidateQueries({ queryKey: ["drafts"] });
      qc.invalidateQueries({ queryKey: ["sources"] });
    }
  }, [crawling, plansFound, qc]);

  // Anything that finished since the last poll: refresh, and report the ones we started.
  useEffect(() => {
    if (!activity) return;
    const finished = activity.recent;
    if (seenDone.current === null) {
      seenDone.current = new Set(finished.map((j) => j.id));
      return;
    }
    let changed = false;
    for (const job of finished) {
      if (seenDone.current.has(job.id)) continue;
      seenDone.current.add(job.id);
      changed = true;
      const t = tracked[job.id];
      if (t) {
        setTracked(({ [job.id]: _, ...rest }) => rest);
        if (!t.quiet) {
          if (job.status === "done") toast.success(`${t.label} finished`, job.kind === "crawl" ? crawlSummary(job) : undefined);
          else if (job.status === "failed") toast.error(`${t.label} failed`, job.message ?? undefined);
        }
        callbacks.current[job.id]?.(job);
        delete callbacks.current[job.id];
      }
    }
    if (changed) qc.invalidateQueries();
  }, [activity, tracked, qc, toast]);

  const track = useCallback(
    (jobId: string, label: string, opts?: TrackOptions | ((job: Job) => void)) => {
      const o = typeof opts === "function" ? { onDone: opts } : (opts ?? {});
      setTracked((t) => ({ ...t, [jobId]: { label, quiet: o.quiet } }));
      if (o.onDone) callbacks.current[jobId] = o.onDone;
      qc.invalidateQueries({ queryKey: ["activity"] });
    },
    [qc],
  );

  const value = useMemo(() => ({ track, active, activity, panelOpen, setPanelOpen }), [track, active, activity, panelOpen]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useJobs() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useJobs outside JobsProvider");
  return ctx;
}
