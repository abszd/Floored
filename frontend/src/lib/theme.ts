import { useEffect, useState } from "react";

export type Theme = "system" | "light" | "dark";
const KEY = "floored.theme";

function read(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export function isDark(theme: Theme = read()): boolean {
  return theme === "dark" || (theme === "system" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
}

/** Light, dark, or follow the computer. index.html applies it before the first paint. */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(read);
  useEffect(() => {
    const apply = () => document.documentElement.classList.toggle("dark", isDark(theme));
    apply();
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* private window: the choice lasts this visit */
    }
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    mq?.addEventListener("change", apply);
    return () => mq?.removeEventListener("change", apply);
  }, [theme]);
  return [theme, setTheme] as const;
}
