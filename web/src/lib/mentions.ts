/** Regex matching `@Name` tokens for the given display names.
 * Names are matched longest-first, case-insensitively, not inside
 * words/emails (`x@y`) and not as a prefix of a longer word.
 */
export function mentionRegex(names: string[]): RegExp | null {
  const escaped = names
    .map((n) => n.trim())
    .filter(Boolean)
    .map(escapeRe)
    .sort((a, b) => b.length - a.length)
  if (!escaped.length) return null
  return new RegExp(
    `(?<![\\p{L}\\p{N}_])@(?:${escaped.join('|')})(?![\\p{L}\\p{N}_])`,
    'giu',
  )
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
