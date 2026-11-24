const KEY = 'concierge.sidebarWidth'
export const SIDEBAR_MIN = 220
export const SIDEBAR_MAX = 560

export const clampSidebar = (w: number): number =>
  Math.round(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, w)))

export function loadSidebarWidth(): number | null {
  try {
    const n = Number(localStorage.getItem(KEY))
    return Number.isFinite(n) && n > 0 ? clampSidebar(n) : null
  } catch {
    return null
  }
}

export function saveSidebarWidth(w: number | null): void {
  try {
    if (w === null) localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, String(w))
  } catch {
    /* storage unavailable — width just won't persist */
  }
}
