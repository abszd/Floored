import { useEffect, type ReactNode } from "react";
import { X, Loader2 } from "lucide-react";
import clsx from "clsx";
import type { FloorStatus } from "../lib/types";

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4 backdrop-blur-sm" onMouseDown={onClose}>
      <div
        className={clsx("panel max-h-[90vh] w-full overflow-auto", wide ? "max-w-3xl" : "max-w-lg")}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
          <h2 className="text-base font-semibold">{title}</h2>
          <button className="btn-ghost -mr-2 p-1.5" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={clsx("h-4 w-4 animate-spin", className)} />;
}

const STATUS: Record<FloorStatus, { label: string; cls: string }> = {
  pending: { label: "Queued", cls: "bg-slate-100 text-slate-600" },
  processing: { label: "Finding rooms…", cls: "bg-sky-50 text-sky-700" },
  placed: { label: "On map", cls: "bg-emerald-50 text-emerald-700" },
  needs_review: { label: "Check placement", cls: "bg-amber-50 text-amber-700" },
  error: { label: "Failed", cls: "bg-rose-50 text-rose-700" },
};

export function StatusBadge({ status }: { status: FloorStatus }) {
  const s = STATUS[status] ?? STATUS.pending;
  return <span className={clsx("inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium", s.cls)}>{s.label}</span>;
}

export function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = value >= 0.85 ? "bg-emerald-500" : value >= 0.6 ? "bg-amber-500" : "bg-rose-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-100">
        <div className={clsx("h-full rounded-full", color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums text-slate-500">{pct}%</span>
    </div>
  );
}

export function Empty({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center px-6 py-10 text-center">
      <div className="mb-3 rounded-full bg-indigo-50 p-3 text-indigo-600">{icon}</div>
      <div className="font-medium">{title}</div>
      {children && <div className="mt-1 text-sm text-slate-500">{children}</div>}
    </div>
  );
}
