import tailwindcss from "@tailwindcss/vite";
import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig } from "vite";

// The node requires its API key on every proxied route, and the dev server is
// not the page that holds one, so the key comes from the environment. Leave
// EXO_API_KEY unset only when the node runs with EXO_API_AUTH_DISABLED=true.
const apiKey = process.env.EXO_API_KEY;
const target = "http://localhost:52415";
const proxy = {
  target,
  changeOrigin: true,
  headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : undefined,
};

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    proxy: {
      "/v1": proxy,
      "/state": proxy,
      "/models": proxy,
      "/instance": proxy,
    },
  },
});
