import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// The build lands inside the Python package, and the built assets are committed.
// That is what keeps `pip install` enough for anyone cloning trance: they get a
// working UI without ever installing node. The cost is ours — rebuild before
// committing — and it is the right way round.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  base: "/static/",
  build: {
    outDir: fileURLToPath(new URL("../src/trance/server/ui", import.meta.url)),
    emptyOutDir: true,
    // Off for the committed build. A 1.4MB source map that changes on every
    // build is pure churn in a repository that carries its own build output,
    // and `npm run dev` gives a better debugging experience than a shipped map
    // ever would.
    sourcemap: false,
  },
  server: {
    // Development runs against a real trance, so the UI is always built against
    // real data rather than a fixture that quietly drifts from the API.
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8080", ws: true },
    },
  },
});
