import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Activity as ActivityIcon, AlertCircle, Ban, Check, CheckCircle2, Circle, Clock, ExternalLink, FileSearch, Globe, Layers, Square, X } from "lucide-react";
import clsx from "clsx";
import { api } from "../lib/api";
import { crawlSummary, useJobs } from "../lib/jobs";
import type { Job } from "../lib/types";
import { Spinner } from "./ui";
import { useToast } from "./Toast";

/** The server stores UTC without an offset; read it as UTC. */
export function parseTime(t: string | null | undefined): number | null {
  if (!t) return null;
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(t) ? t : `${t}Z`);
}

export function duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

function ago(t: string | null): string {
  const ms = Date.now() - (parseTime(t) ?? Date.now());
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)} min ago`;
  return `${Math.floor(ms / 3_600_000)} h ago`;
}

function useNow(every = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), every);
    return () => clearInterval(t);
  }, [every]);
  return now;
}

/** Header button: shows what's running and opens the activity panel. */
export function ActivityButton() {
  const { activity, panelOpen, setPanelOpen } = useJobs();
  const running = activity?.running.length ?? 0;
  const queued = activity?.queued.length ?? 0;
  const crawl = activity?.running.find((j) => j.kind === "crawl");
  return (
    <button
      className={clsx("btn-ghost relative text-xs", panelOpen && "bg-slate-100")}
      onClick={() => setPanelOpen(!panelOpen)}
      title="What Floored is working on"
    >
      {running ? <Spinner className="text-indigo-600" /> : <ActivityIcon className="h-4 w-4" />}
      <span className="hidden sm:inline">
        {running ? (crawl ? `${crawl.label} · ${Math.round(crawl.progress * 100)}%` : `${running} running`) : "Activity"}
      </span>
      {running + queued > 0 && (
        <span className="rounded-full bg-indigo-600 px-1.5 text-[10px] font-semibold leading-4 text-white">{running + queued}</span>
      )}
    </button>
  );
}

export default function ActivityPanel() {
  const { activity, panelOpen, setPanelOpen } = useJobs();
  useEffect(() => {
    if (!panelOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPanelOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panelOpen, setPanelOpen]);
  if (!panelOpen) return null;
  const running = activity?.running ?? [];
  const queued = activity?.queued ?? [];
  const recent = activity?.recent ?? [];

  return createPortal(
    <aside className="panel fixed bottom-3 right-3 top-16 z-40 flex w-[min(26rem,calc(100vw-1.5rem))] flex-col overflow-hidden" aria-label="Activity">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <div>
          <h2 className="text-base font-semibold">Activity</h2>
          <div className="text-xs text-slate-500">
            {running.length} running · {queued.length} waiting
            {activity && ` · ${activity.workers} at a time`}
          </div>
        </div>
        <button className="btn-ghost -mr-2 p-1.5" onClick={() => setPanelOpen(false)} aria-label="Close">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        {!running.length && !queued.length && !recent.length && (
          <div className="py-10 text-center text-sm text-slate-500">Nothing running. Crawls and floor plan conversions show up here.</div>
        )}
        {running.length > 0 && (
          <Section title="Running now">
            {running.map((j) => (j.kind === "crawl" ? <CrawlCard key={j.id} job={j} /> : <SimpleRunning key={j.id} job={j} />))}
          </Section>
        )}
        {queued.length > 0 && (
          <Section title="Waiting">
            {queued.map((j, i) => (
              <QueuedRow key={j.id} job={j} position={i} />
            ))}
          </Section>
        )}
        {recent.length > 0 && (
          <Section title="Finished in the last day">
            {recent.map((j) => (
              <RecentRow key={j.id} job={j} />
            ))}
          </Section>
        )}
      </div>
    </aside>,
    document.body,
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">{title}</h3>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function useStop(job: Job) {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: () => api.stopJob(job.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["activity"] }),
    onError: (e: Error) => toast.error("Couldn't stop it", e.message),
  });
}

const STEPS = [
  { key: "reading", label: "Read pages" },
  { key: "checking", label: "Check files" },
  { key: "matching", label: "Find buildings" },
] as const;

/** A crawl, step by step: pages read, files checked, buildings looked up. */
export function CrawlCard({ job, compact }: { job: Job; compact?: boolean }) {
  const now = useNow();
  const stop = useStop(job);
  const d = job.detail;
  const phase = d.phase ?? "reading";
  const at = phase === "finished" ? 3 : STEPS.findIndex((s) => s.key === phase);
  const started = parseTime(job.started_at);
  const counts = [
    `${d.pages_read ?? 0} of up to ${d.max_pages ?? "?"} pages`,
    d.files_total ? `${d.files_checked ?? 0} of ${d.files_total} files` : `${d.files_found ?? 0} files spotted`,
    d.buildings_found ? `${d.buildings_matched ?? 0} of ${d.buildings_found}` : "",
  ];
  const stepProgress = [
    Math.min(1, (d.pages_read ?? 0) / Math.max(1, d.max_pages ?? 1)),
    d.files_total ? (d.files_checked ?? 0) / d.files_total : 0,
    d.buildings_found ? (d.buildings_matched ?? 0) / d.buildings_found : 0,
  ];

  return (
    <div className={clsx("rounded-xl border border-indigo-100 bg-indigo-50/40", compact ? "p-2.5" : "p-3")}>
      {!compact && (
        <div className="flex items-start gap-2">
          <Globe className="mt-0.5 h-4 w-4 shrink-0 text-indigo-500" />
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{job.label}</div>
            <div className="text-[11px] text-slate-500">
              {started ? `Running for ${duration(now - started)}` : "Starting…"} · {Math.round(job.progress * 100)}%
            </div>
          </div>
          <StopButton job={job} stop={stop} />
        </div>
      )}

      <ol className={clsx("grid grid-cols-3 gap-2", !compact && "mt-3")}>
        {STEPS.map((s, i) => {
          const state = i < at ? "done" : i === at ? "active" : "todo";
          return (
            <li key={s.key} className="min-w-0">
              <div className="flex items-center gap-1 text-[11px] font-medium">
                {state === "done" ? (
                  <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
                ) : state === "active" ? (
                  <Spinner className="h-3.5 w-3.5 shrink-0 text-indigo-600" />
                ) : (
                  <Circle className="h-3.5 w-3.5 shrink-0 text-slate-300" />
                )}
                <span className={clsx("truncate", state === "todo" ? "text-slate-400" : "text-slate-700")}>{s.label}</span>
              </div>
              <div className="mt-1 h-1 overflow-hidden rounded-full bg-white">
                <div
                  className={clsx("h-full rounded-full transition-all", state === "done" ? "bg-emerald-400" : "bg-indigo-500")}
                  style={{ width: `${state === "done" ? 100 : state === "active" ? Math.max(4, stepProgress[i] * 100) : 0}%` }}
                />
              </div>
              <div className="mt-0.5 truncate text-[10px] tabular-nums text-slate-500">{state === "todo" ? "" : counts[i]}</div>
            </li>
          );
        })}
      </ol>

      <div className="mt-2 flex flex-wrap gap-1.5">
        <Stat icon={<Layers className="h-3 w-3" />} value={d.plans_found ?? 0} label="floor plans" />
        <Stat icon={<FileSearch className="h-3 w-3" />} value={d.buildings_found ?? 0} label="buildings" />
        {phase === "reading" && d.pages_waiting ? <Stat icon={<Clock className="h-3 w-3" />} value={d.pages_waiting} label="links queued" /> : null}
      </div>

      {d.current && (
        <div className="mt-2 flex min-w-0 items-center gap-1 text-[11px] text-slate-500">
          <span className="shrink-0">{phase === "reading" ? "Reading" : phase === "checking" ? "Checking" : "Looking up"}</span>
          {d.current_url ? (
            <a href={d.current_url} target="_blank" rel="noreferrer" className="inline-flex min-w-0 items-center gap-0.5 font-medium text-slate-700 hover:underline">
              <span className="truncate">{d.current}</span>
              <ExternalLink className="h-3 w-3 shrink-0" />
            </a>
          ) : (
            <span className="truncate font-medium text-slate-700">{d.current}</span>
          )}
        </div>
      )}
      {job.stop_requested && <div className="mt-1 text-[11px] text-amber-700">Stopping. Plans found so far are kept.</div>}
      {compact && (
        <div className="mt-2 flex justify-end">
          <StopButton job={job} stop={stop} />
        </div>
      )}
    </div>
  );
}

function StopButton({ job, stop }: { job: Job; stop: ReturnType<typeof useStop> }) {
  if (job.stop_requested) return null;
  return (
    <button
      className="btn-ghost shrink-0 px-2 py-1 text-[11px] text-slate-500"
      disabled={stop.isPending}
      onClick={() => confirm("Stop this crawl now? The plans it found so far are kept and still matched to buildings.") && stop.mutate()}
    >
      <Square className="h-3 w-3" /> Stop
    </button>
  );
}

function Stat({ icon, value, label }: { icon: ReactNode; value: number; label: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-white px-2 py-0.5 text-[11px] text-slate-600 ring-1 ring-slate-200">
      {icon}
      <span className="font-semibold tabular-nums text-slate-800">{value}</span> {label}
    </span>
  );
}

function SimpleRunning({ job }: { job: Job }) {
  const now = useNow();
  const started = parseTime(job.started_at);
  return (
    <div className="rounded-xl border border-slate-200 p-3">
      <div className="flex items-center gap-2">
        <Spinner className="shrink-0 text-indigo-600" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{job.label}</div>
          <div className="truncate text-[11px] text-slate-500">
            {job.message ?? "Working…"}
            {started ? ` · ${duration(now - started)}` : ""}
          </div>
        </div>
      </div>
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-slate-100">
        <div className="h-full animate-pulse rounded-full bg-indigo-400" style={{ width: `${Math.max(15, job.progress * 100)}%` }} />
      </div>
    </div>
  );
}

function QueuedRow({ job, position }: { job: Job; position: number }) {
  const stop = useStop(job);
  return (
    <div className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-slate-50">
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[10px] font-semibold text-slate-500">{position + 1}</span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm">{job.label}</div>
        <div className="text-[11px] text-slate-400">{position === 0 ? "Next up" : `${position} ahead of it`}</div>
      </div>
      <button className="btn-ghost px-1.5 py-1 text-[11px] text-slate-400" title="Cancel" disabled={stop.isPending} onClick={() => stop.mutate()}>
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function RecentRow({ job }: { job: Job }) {
  const { setPanelOpen } = useJobs();
  const started = parseTime(job.started_at);
  const finished = parseTime(job.finished_at);
  const summary = job.status === "failed" ? job.message : job.kind === "crawl" ? crawlSummary(job) : job.status === "cancelled" ? "Cancelled" : null;
  const toReview = job.kind === "crawl" ? Number((job.result as { to_review?: number }).to_review ?? 0) : 0;
  return (
    <div className="flex items-start gap-2 rounded-lg px-2 py-1.5">
      {job.status === "done" ? (
        <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
      ) : job.status === "failed" ? (
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-500" />
      ) : (
        <Ban className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
      )}
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm">{job.label}</div>
        {summary && <div className={clsx("line-clamp-2 text-[11px]", job.status === "failed" ? "text-rose-600" : "text-slate-500")}>{summary}</div>}
        <div className="text-[10px] text-slate-400">
          {ago(job.finished_at)}
          {started && finished ? ` · took ${duration(finished - started)}` : ""}
        </div>
      </div>
      {toReview > 0 && (
        <Link to="/discover" onClick={() => setPanelOpen(false)} className="shrink-0 text-[11px] font-medium text-indigo-600 hover:underline">
          Review {toReview}
        </Link>
      )}
    </div>
  );
}
