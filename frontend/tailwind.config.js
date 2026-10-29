/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
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
