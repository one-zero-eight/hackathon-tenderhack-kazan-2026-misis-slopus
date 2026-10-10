import { defineConfig } from "vite";
import { nitro } from "nitro/vite";
import { solidStart } from "@solidjs/start/config";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [{
    name: "nitro-vite-module-imports",
    enforce: "pre",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        // Solid API routes live behind Nitro's catch-all handler. Treat image
        // requests as API fetches so Nitro does not send them to Vite's assets.
        if (req.url?.startsWith("/api/search/")) req.headers["sec-fetch-dest"] = "empty";
        // LAN HTTP has no Sec-Fetch-Dest: Nitro otherwise renders ?import JSON as HTML.
        // Explicit Vite module imports belong to Vite, not Nitro's SPA renderer.
        if (!req.headers["sec-fetch-dest"] && req.url
          && new URL(req.url, "http://localhost").searchParams.has("import")) {
          (req as typeof req & { _nitroHandled?: boolean })._nitroHandled = true;
        }
        next();
      });
    },
  }, solidStart({ ssr: false, devOverlay: false }), tailwindcss(), nitro()],
  ssr: { external: ["better-sqlite3"] },
});
