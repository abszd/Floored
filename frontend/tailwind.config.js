/** @type {import('tailwindcss').Config} */
// Slate is read from CSS variables so dark mode can flip the whole grey scale at once
// (see index.css); every bg-slate-*, text-slate-* and border-slate-* follows.
const slate = Object.fromEntries(
  [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((n) => [n, `rgb(var(--slate-${n}) / <alpha-value>)`]),
);

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: { slate },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
      boxShadow: {
        panel: "0 1px 2px rgb(0 0 0 / 0.06), 0 8px 24px rgb(15 23 42 / 0.10)",
      },
    },
  },
  plugins: [],
};
