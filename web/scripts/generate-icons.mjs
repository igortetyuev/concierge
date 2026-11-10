// Generate PWA PNG icons from public/favicon.svg via headless Chrome.
// Usage: node scripts/generate-icons.mjs   (CHROME env overrides the binary)
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const svg = readFileSync(join(root, 'public/favicon.svg'), 'utf8')
const outDir = join(root, 'public/icons')
const tmpDir = join(root, 'node_modules/.tmp-icons')
mkdirSync(outDir, { recursive: true })
mkdirSync(tmpDir, { recursive: true })

const CHROME =
  process.env.CHROME ??
  [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
  ].find((p) => existsSync(p))

if (!CHROME) throw new Error('Chrome/Chromium not found; set CHROME env')

// `scale` shrinks the artwork inside a flat-color canvas (maskable safe zone).
function page(size, scale) {
  return `<!doctype html><html><body style="margin:0;width:${size}px;height:${size}px;background:#6d28d9;display:grid;place-items:center;overflow:hidden"><div style="width:${size * scale}px;height:${size * scale}px">${svg.replace(/<svg /, `<svg width="${size * scale}" height="${size * scale}" `)}</div></body></html>`
}

const targets = [
  ['icon-192.png', 192, 1],
  ['icon-512.png', 512, 1],
  ['maskable-512.png', 512, 0.66],
  ['apple-touch-icon.png', 180, 1],
]

for (const [name, size, scale] of targets) {
  const html = join(tmpDir, `${name}.html`)
  writeFileSync(html, page(size, scale))
  execFileSync(CHROME, [
    '--headless',
    '--disable-gpu',
    '--no-first-run',
    `--screenshot=${join(outDir, name)}`,
    `--window-size=${size},${size}`,
    `--default-background-color=00000000`,
    `file://${html}`,
  ])
  console.log('wrote', name)
}
rmSync(tmpDir, { recursive: true, force: true })
