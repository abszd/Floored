import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { FileUp, Link2 } from "lucide-react";
import { api } from "../lib/api";
import { useJobs } from "../lib/jobs";
import type { Building } from "../lib/types";
import { Modal, Spinner } from "./ui";
import { useToast } from "./Toast";

/** Upload or link a floor plan; conversion and placement run in the background. */
export default function AddFloorDialog({ building, onClose }: { building: Building; onClose: () => void }) {
  const toast = useToast();
  const { track } = useJobs();
  const input = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<"file" | "url">("file");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [drag, setDrag] = useState(false);
  const nextLevel = building.floors.length ? Math.max(...building.floors.map((f) => f.level)) + 1 : 1;
  const [level, setLevel] = useState(String(nextLevel));
  const [name, setName] = useState("");
  const [page, setPage] = useState("1");

  const isPdf = mode === "file" ? file?.type === "application/pdf" : url.toLowerCase().includes(".pdf");

  const add = useMutation({
    mutationFn: () =>
      api.addFloor(building.id, {
        file: mode === "file" ? file ?? undefined : undefined,
        url: mode === "url" ? url : undefined,
        level: Number(level),
        name: name.trim() || undefined,
        page: isPdf ? Number(page) || 1 : undefined,
      }),
    onSuccess: ({ floor, job_id }) => {
      track(job_id, `${building.name} · ${floor.name}`);
      toast.success("Floor plan added", "Finding rooms and placing it on the building. This takes a few seconds.");
      onClose();
    },
    onError: (e: Error) => toast.error("Couldn't add floor plan", e.message),
  });

  const ready = mode === "file" ? !!file : /^https?:\/\//.test(url);

  return (
    <Modal title={`Add a floor plan to ${building.name}`} onClose={onClose}>
      <div className="mb-4 inline-flex rounded-lg bg-slate-100 p-1 text-sm">
        {(["file", "url"] as const).map((m) => (
          <button key={m} onClick={() => setMode(m)} className={`rounded-md px-3 py-1 ${mode === m ? "bg-white font-medium shadow-sm" : "text-slate-500"}`}>
            {m === "file" ? "Upload" : "From a link"}
          </button>
        ))}
      </div>

      {mode === "file" ? (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            const f = e.dataTransfer.files[0];
            if (f) setFile(f);
          }}
          onClick={() => input.current?.click()}
          className={`flex cursor-pointer flex-col items-center rounded-xl border-2 border-dashed px-4 py-8 text-center transition ${drag ? "border-indigo-400 bg-indigo-50" : "border-slate-200 hover:border-slate-300"}`}
        >
          <FileUp className="mb-2 h-6 w-6 text-indigo-500" />
          {file ? (
            <div className="text-sm font-medium">{file.name}</div>
          ) : (
            <>
              <div className="text-sm font-medium">Drop an image or PDF here</div>
              <div className="text-xs text-slate-500">or click to choose a file</div>
            </>
          )}
          <input ref={input} type="file" accept="image/*,application/pdf" className="hidden" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </div>
      ) : (
        <div className="relative">
          <Link2 className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
          <input className="input pl-9" placeholder="https://…/floor-2.pdf" value={url} onChange={(e) => setUrl(e.target.value)} />
        </div>
      )}

      <div className="mt-4 grid grid-cols-3 gap-3">
        <div>
          <label className="label">Level</label>
          <input type="number" className="input" value={level} onChange={(e) => setLevel(e.target.value)} />
        </div>
        <div className={isPdf ? "" : "col-span-2"}>
          <label className="label">Name (optional)</label>
          <input className="input" placeholder="e.g. 2nd floor" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        {isPdf && (
          <div>
            <label className="label">PDF page</label>
            <input type="number" min={1} className="input" value={page} onChange={(e) => setPage(e.target.value)} />
          </div>
        )}
      </div>
      <p className="mt-2 text-xs text-slate-500">Use −1 for a basement and 0 for a ground floor below the 1st.</p>

      <div className="mt-5 flex justify-end gap-2">
        <button className="btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <button className="btn-primary" disabled={!ready || add.isPending} onClick={() => add.mutate()}>
          {add.isPending && <Spinner />} Add and process
        </button>
      </div>
    </Modal>
  );
}
