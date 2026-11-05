import { useEffect, useRef, useState, type InputHTMLAttributes } from "react";
import { Building2, MapPin } from "lucide-react";
import clsx from "clsx";
import { api } from "../lib/api";
import type { Place } from "../lib/types";

interface Props extends Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "onSelect"> {
  value: string;
  onChange: (value: string) => void;
  /** Called when a suggestion is picked (click, or arrows + Enter). */
  onPick: (place: Place) => void;
  /** Prefer suggestions near a town ("Houghton, MI") or a point. */
  near?: { hint?: string | null; lat?: number; lon?: number };
}

/** A text box that suggests addresses and places from OpenStreetMap as you type. */
export default function AddressInput({ value, onChange, onPick, near, className, onKeyDown, ...rest }: Props) {
  const [items, setItems] = useState<Place[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [typed, setTyped] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  // Ask only after the user has typed something and paused for a moment.
  useEffect(() => {
    if (!typed || value.trim().length < 3) {
      setItems([]);
      return;
    }
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      api
        .suggest(value.trim(), near ?? {}, ctrl.signal)
        .then((r) => {
          setItems(r);
          setActive(-1);
          setOpen(true);
        })
        .catch(() => {});
    }, 300);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [value, typed, near?.hint, near?.lat, near?.lon]);

  useEffect(() => {
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const pick = (p: Place) => {
    onChange(p.display_name);
    setTyped(false);
    setOpen(false);
    setItems([]);
    onPick(p);
  };

  return (
    <div ref={box} className="relative flex-1">
      <input
        {...rest}
        className={clsx("input", className)}
        value={value}
        autoComplete="off"
        role="combobox"
        aria-expanded={open && items.length > 0}
        onChange={(e) => {
          onChange(e.target.value);
          setTyped(true);
        }}
        onFocus={() => items.length && setOpen(true)}
        onKeyDown={(e) => {
          if (open && items.length) {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              const step = e.key === "ArrowDown" ? 1 : -1;
              setActive((i) => (i + step + items.length) % items.length);
              return;
            }
            if (e.key === "Enter" && active >= 0) {
              e.preventDefault();
              pick(items[active]);
              return;
            }
            if (e.key === "Escape") {
              e.stopPropagation();
              setOpen(false);
              return;
            }
          }
          onKeyDown?.(e);
        }}
      />
      {open && items.length > 0 && (
        <ul role="listbox" className="absolute left-0 right-0 top-full z-20 mt-1 max-h-64 overflow-auto rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
          {items.map((p, i) => {
            const [first, ...rest] = p.display_name.split(", ");
            const isBuilding = p.category === "building" || !!p.address?.housenumber;
            return (
              <li key={`${p.osm_id}-${i}`} role="option" aria-selected={i === active}>
                <button
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(p)}
                  className={clsx("flex w-full items-start gap-2.5 px-3 py-2 text-left text-sm", i === active && "bg-indigo-50")}
                >
                  {isBuilding ? (
                    <Building2 className="mt-0.5 h-4 w-4 shrink-0 text-indigo-500" />
                  ) : (
                    <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
                  )}
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{first}</span>
                    <span className="block truncate text-xs text-slate-500">{rest.join(", ")}</span>
                  </span>
                </button>
              </li>
            );
          })}
          <li className="px-3 pb-1 pt-1.5 text-[10px] text-slate-400">Suggestions from OpenStreetMap via Photon</li>
        </ul>
      )}
    </div>
  );
}
