import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { Job } from "./types";
import { useToast } from "../components/Toast";

interface JobsContext {
  track: (jobId: string, label: string, onDone?: (job: Job) => void) => void;
  active: Job[];
  labels: Record<string, string>;
}

const Ctx = createContext<JobsContext | null>(null);

/** Polls running jobs, refreshes data when they finish, and reports the outcome. */
export function JobsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [tracked, setTracked] = useState<Record<string, string>>({});
  const callbacks = useRef<Record<string, (job: Job) => void>>({});

  const { data: active = [] } = useQuery({
    queryKey: ["jobs", "active"],
    queryFn: () => api.jobs(true),
    refetchInterval: (q) => ((q.state.data?.length ?? 0) > 0 || Object.keys(tracked).length > 0 ? 1500 : 15000),
  });

  // Anything we tracked that is no longer active has finished: fetch its outcome.
  useEffect(() => {
    const activeIds = new Set(active.map((j) => j.id));
    for (const id of Object.keys(tracked)) {
      if (activeIds.has(id)) continue;
      api.job(id).then((job) => {
        if (job.status === "queued" || job.status === "running") return;
        const label = tracked[id];
        setTracked(({ [id]: _, ...rest }) => rest);
        qc.invalidateQueries();
        if (job.status === "done") toast.success(`${label} finished`);
        else toast.error(`${label} failed`, job.message ?? undefined);
        callbacks.current[id]?.(job);
        delete callbacks.current[id];
      });
    }
  }, [active, tracked, qc, toast]);

  const track = useCallback(
    (jobId: string, label: string, onDone?: (job: Job) => void) => {
      setTracked((t) => ({ ...t, [jobId]: label }));
      if (onDone) callbacks.current[jobId] = onDone;
      qc.invalidateQueries({ queryKey: ["jobs", "active"] });
    },
    [qc],
  );

  const value = useMemo(() => ({ track, active, labels: tracked }), [track, active, tracked]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useJobs() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useJobs outside JobsProvider");
  return ctx;
}

export const JOB_LABELS: Record<string, string> = {
  crawl: "Crawl",
  process_floor: "Floor plan conversion",
  approve_candidate: "Floor plan conversion",
  match_candidates: "Building matching",
};
