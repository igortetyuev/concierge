import { useState } from 'react'

import { apiClient } from '../lib/api'
import type { MessagePart } from '../lib/api'
import Markdown from './Markdown'

interface ChecklistItemData {
  id: string
  text: string
  cells?: string[]
  checked?: boolean
}

function checkedIds(items: ChecklistItemData[]): Set<string> {
  return new Set(
    items.filter((it) => it.checked === true).map((it) => String(it.id)),
  )
}

function sameSet(a: Set<string>, b: Set<string>): boolean {
  return a.size === b.size && [...a].every((x) => b.has(x))
}

/** `checklist` part (show_checklist): tickable list or table + a submit
 * button. Resubmission is allowed — the stored state comes back via
 * `message.updated`, which resets the draft below. */
export default function ChecklistPart({
  part,
  convId,
  messageId,
}: {
  part: MessagePart
  convId?: number
  messageId?: number
}) {
  const items = (part.items as ChecklistItemData[] | undefined) ?? []
  const columns = (part.columns as string[] | undefined) ?? []
  const submissions = Number(part.submissions ?? 0)
  // Signature of the stored checkbox state: changes on every submission
  // (ours reflected via message.updated, or another client's) → reset draft.
  const storeKey = `${submissions}:${items
    .map((it) => `${it.id}=${it.checked === true ? 1 : 0}`)
    .join(',')}`
  const [draft, setDraft] = useState<Set<string>>(() => checkedIds(items))
  const [key, setKey] = useState(storeKey)
  const [sent, setSent] = useState<Set<string> | null>(null)
  const [optimistic, setOptimistic] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  if (key !== storeKey) {
    setKey(storeKey)
    setDraft(checkedIds(items))
    setSent(null)
    setOptimistic(0)
  }

  const interactive = convId != null && messageId != null
  const baseline = sent ?? checkedIds(items)
  const everSubmitted = submissions > 0 || sent != null
  // Stored count wins once message.updated lands; before that the submit's
  // own +1 keeps the counter right (no double-count on the echo).
  const submitCount = Math.max(submissions, optimistic)

  function toggle(id: string) {
    setDraft((cur) => {
      const next = new Set(cur)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function submit() {
    if (!interactive || convId == null || messageId == null) return
    setBusy(true)
    setError('')
    try {
      await apiClient.submitChecklist(
        convId,
        messageId,
        String(part.checklist_id ?? ''),
        [...draft],
      )
      setSent(new Set(draft))
      if (!everSubmitted || !sameSet(draft, baseline)) {
        setOptimistic((cur) => Math.max(cur, submissions) + 1)
      }
    } catch {
      setError('Не удалось отправить')
    } finally {
      setBusy(false)
    }
  }

  const box = (it: ChecklistItemData, label?: string) => {
    const id = String(it.id)
    return (
      <input
        type="checkbox"
        checked={draft.has(id)}
        disabled={!interactive || busy}
        aria-label={label}
        onChange={() => toggle(id)}
      />
    )
  }

  return (
    <div className={`checklist-card${columns.length > 0 ? ' table' : ''}`}>
      {part.title ? (
        <div className="checklist-title">{String(part.title)}</div>
      ) : null}
      {columns.length > 0 ? (
        <div className="checklist-table-wrap">
          <table className="checklist-table">
            <thead>
              <tr>
                <th className="checklist-check" />
                {columns.map((c, ci) => (
                  <th key={ci}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr
                  key={String(it.id)}
                  className={draft.has(String(it.id)) ? 'checked' : ''}
                >
                  <td className="checklist-check">
                    {box(it, String(it.text || it.id))}
                  </td>
                  {(it.cells ?? []).map((cell, ci) => (
                    <td key={ci}>
                      <Markdown text={cell} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <ul className="checklist-items">
          {items.map((it) => (
            <li key={String(it.id)}>
              <label
                className={`checklist-item${draft.has(String(it.id)) ? ' checked' : ''}`}
              >
                {box(it)}
                <span>{it.text}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {interactive && (
        <button className="btn primary" onClick={() => void submit()}>
          {String(part.submit_label || 'Отправить')}
        </button>
      )}
      {submitCount > 0 && (
        <span className="checklist-note muted">Отправлено ×{submitCount}</span>
      )}
      {error && <div className="auth-error">{error}</div>}
    </div>
  )
}
