import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
import { defineConfig } from 'vite'

// Backend origin for dev/preview proxies (production: Caddy does the same split).
const apiTarget = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

const apiProxy = {
  target: apiTarget,
  changeOrigin: true,
  ws: true,
  rewrite: (path: string) => path.replace(/^\/api/, ''),
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: [
        'favicon.svg',
        'icons/apple-touch-icon.png',
        'fonts/InterVariable.woff2',
      ],
      manifest: {
        name: 'Консьерж',
        short_name: 'Консьерж',
        description: 'Семейный агент-консьерж',
        theme_color: '#317cff',
        background_color: '#141414',
        display: 'standalone',
        start_url: '/',
        icons: [
          { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'icons/maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        navigateFallback: '/index.html',
        // /api — backend; /myschool — third-party static subsite (.keep dir);
        // /root.crt — CA cert users download to trust `tls internal`.
        navigateFallbackDenylist: [/^\/api\//, /^\/myschool(\/|$)/, /^\/root\.crt$/],
      },
    }),
  ],
  server: {
    proxy: { '/api': apiProxy },
  },
  preview: {
    proxy: { '/api': apiProxy },
  },
})
