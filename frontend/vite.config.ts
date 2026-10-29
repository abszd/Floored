import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.FLOORED_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  // GitHub Pages serves the site from /<repo>/; the deploy workflow sets this.
  base: process.env.VITE_BASE ?? "/",
  plugins: [react()],
  server: {
    proxy: { "/api": api, "/media": api },
  },
});
