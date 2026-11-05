import { NavLink, Route, Routes } from "react-router-dom";
import { Map as MapIcon, Monitor, Moon, Radar, Sun } from "lucide-react";
import { useTheme } from "./lib/theme";
import MapPage from "./pages/MapPage";
import DiscoverPage from "./pages/DiscoverPage";
import ActivityPanel, { ActivityButton } from "./components/ActivityPanel";
import { useQuery } from "@tanstack/react-query";
import { ServerOff } from "lucide-react";
import { api } from "./lib/api";

/** The static site is useless without the API; say so plainly instead of showing empty lists. */
function BackendBanner() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, retry: 1, refetchInterval: (q) => (q.state.error ? 15000 : false) });
  if (!health.error) return null;
  return (
    <div className="z-20 flex items-center gap-2 border-b border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">
      <ServerOff className="h-4 w-4 shrink-0" />
      Can't reach the Floored server{import.meta.env.VITE_API_URL ? ` at ${import.meta.env.VITE_API_URL}` : ""}. The map works, but buildings and floor plans won't load until it's running.
    </div>
  );
}

const nav = ({ isActive }: { isActive: boolean }) =>
  `inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition ${isActive ? "bg-slate-100 text-slate-900" : "text-slate-500 hover:text-slate-800"}`;

function ThemeButton() {
  const [theme, setTheme] = useTheme();
  const next = { system: "light", light: "dark", dark: "system" } as const;
  const Icon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;
  const label = { system: "Theme: follows your computer", light: "Theme: light", dark: "Theme: dark" }[theme];
  return (
    <button className="btn-ghost p-2" onClick={() => setTheme(next[theme])} title={`${label}. Click to change.`} aria-label={label} data-testid="theme">
      <Icon className="h-4 w-4" />
    </button>
  );
}

export default function App() {
  return (
    <div className="flex h-full flex-col">
      <header className="z-20 flex h-14 shrink-0 items-center gap-4 border-b border-slate-200 bg-white px-4">
        <div className="flex items-center gap-2">
          <img src={`${import.meta.env.BASE_URL}favicon.svg`} className="h-7 w-7" alt="" />
          <span className="text-base font-semibold tracking-tight">Floored</span>
        </div>
        <nav className="flex items-center gap-1">
          <NavLink to="/" end className={nav}>
            <MapIcon className="h-4 w-4" /> Map
          </NavLink>
          <NavLink to="/discover" className={nav}>
            <Radar className="h-4 w-4" /> Discover
          </NavLink>
        </nav>
        <div className="flex-1" />
        <ThemeButton />
        <ActivityButton />
      </header>
      <ActivityPanel />
      <BackendBanner />
      <main className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<MapPage />} />
          <Route path="/discover" element={<DiscoverPage />} />
        </Routes>
      </main>
    </div>
  );
}
