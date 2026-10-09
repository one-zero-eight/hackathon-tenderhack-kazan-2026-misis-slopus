import { defineConfig } from "vite";
import { nitro } from "nitro/vite";
import { solidStart } from "@solidjs/start/config";
import tailwindcss from "@tailwindcss/vite";
import { arch, platform } from "node:process";

export default defineConfig({
  plugins: [solidStart({ devOverlay: false }), tailwindcss(), nitro({ traceDeps: [`sqlite-vec-${platform === "win32" ? "windows" : platform}-${arch}*`] })],
  ssr: { external: ["better-sqlite3", "sqlite-vec"] },
});
