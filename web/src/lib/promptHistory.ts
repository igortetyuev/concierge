const KEY = 'concierge.promptHistory'
const MAX = 100

export function loadPromptHistory(): string[] {
  try {
    const raw = localStorage.getItem(KEY)
    const list: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(list)
      ? list.filter((s): s is string => typeof s === 'string')
      : []
  } catch {
    return []
  }
}

export function pushPromptHistory(text: string): void {
  const trimmed = text.trim()
  if (!trimmed) return
  const list = loadPromptHistory()
  if (list[list.length - 1] === trimmed) return
  list.push(trimmed)
  if (list.length > MAX) list.splice(0, list.length - MAX)
  try {
    localStorage.setItem(KEY, JSON.stringify(list))
  } catch {
    /* storage full or unavailable — history just won't persist */
  }
}
