# concierge web

Адаптивный PWA-клиент консьержа: React 19 + Vite + TypeScript, vite-plugin-pwa,
nanostores, react-router.

```bash
pnpm install
pnpm dev        # dev-сервер; /api проксируется на VITE_API_TARGET (default http://localhost:8000)
pnpm lint       # oxlint
pnpm build      # tsc -b && vite build -> dist/
pnpm preview    # preview собранной версии
```

Иконки PWA генерируются из `public/favicon.svg`: `node scripts/generate-icons.mjs`.
