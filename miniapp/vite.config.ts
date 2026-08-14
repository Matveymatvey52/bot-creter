import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served at /app/{bot_id} (see runtime/combined_app.py) — bot_id is a
// runtime path segment, not known at build time. A relative base (`./`)
// would resolve differently depending on whether the browser sees a
// trailing slash on that path, so assets are pinned to a FIXED absolute
// prefix instead (/app-assets/, registered as a static route by
// runtime/miniapp_api.py's register_routes) — the same one build's JS/CSS
// URLs work identically no matter which bot_id the page was loaded under.
export default defineConfig({
  plugins: [react()],
  base: '/app-assets/',
  build: {
    outDir: 'dist',
  },
})
