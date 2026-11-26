import { useEffect, useRef, useState } from 'react'

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

/** `checklist` part (show_checklist): tickable list or table, no submit
 * button — checkbox changes auto-submit the full state after a short
 * debounce (a burst of clicks batches into one submission).
 *
 * Sync model: the server state (part items) is the source of truth and
 * arrives to every open window/device via `message.updated`; local ticks
 * are optimistic deltas in `pending` overlaid on top of it. A delta is
 * dropped once the echoed server state confirms it, so a late echo of an
 * older submission can neither flicker the checkbox nor eat a click, and
 * concurrent edits from another device merge unless they hit the same
 * item (last writer wins there). Submissions are chained so concurrent
 * ones can't reach the server out of order and regress the state. */
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
  const storeKey = `${submissions}:${items
    .map((it) => `${it.id}=${it.checked === true ? 1 : 0}`)
    .join(',')}`
  const serverChecked = checkedIds(items)
  const [pending, setPending] = useState<ReadonlyMap<string, boolean>>(
    new Map(),
  )
  const [error, setError] = useState('')

  const interactive = convId != null && messageId != null
  const pendingRef = useRef(pending)
  const serverRef = useRef(serverChecked)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const chain = useRef<Promise<void>>(Promise.resolve())
  // Submits fired but not yet acknowledged by the server. While non-zero
  // the last echo may lag behind the real server state, so a net-zero
  // overlay must still be sent (the server may hold the opposite value
  // from the in-flight submission).
  const inFlight = useRef(0)

  useEffect(() => {
    pendingRef.current = pending
    serverRef.current = serverChecked
  })

  // Drop optimistic deltas the echoed server state now confirms
  // (state adjustment during render, keyed to the stored-state signature).
  const [pruneKey, setPruneKey] = useState(storeKey)
  if (pruneKey !== storeKey) {
    setPruneKey(storeKey)
    if (pending.size > 0) {
      const next = new Map<string, boolean>()
      let changed = false
      for (const [id, v] of pending) {
        if (serverChecked.has(id) === v) changed = true
        else next.set(id, v)
      }
      if (changed) setPending(next)
    }
  }

  const display = new Set(serverChecked)
  for (const [id, v] of pending) {
    if (v) display.add(id)
    else display.delete(id)
  }

  function fireSubmit() {
    timer.current = null
    if (convId == null || messageId == null) return
    const state = new Set(serverRef.current)
    for (const [id, v] of pendingRef.current) {
      if (v) state.add(id)
      else state.delete(id)
    }
    // Net-zero round trip (ticked and unticked with nothing outstanding):
    // nothing to tell the agent.
    if (sameSet(state, serverRef.current) && inFlight.current === 0) return
    const checked = [...state]
    const checklistId = String(part.checklist_id ?? '')
    inFlight.current += 1
    chain.current = chain.current.then(async () => {
      try {
        await apiClient.submitChecklist(convId, messageId, checklistId, checked)
      } catch {
        setError('Не удалось отправить')
      } finally {
        inFlight.current -= 1
      }
    })
  }

  // Flush a pending submission on unmount (e.g. quick navigation away)
  // instead of dropping the last click.
  const fireRef = useRef(fireSubmit)
  useEffect(() => {
    fireRef.current = fireSubmit
  })
  useEffect(() => {
    return () => {
      if (timer.current != null) {
        clearTimeout(timer.current)
        fireRef.current()
      }
    }
  }, [])

  function toggle(id: string) {
    if (!interactive) return
    setPending((cur) => new Map(cur).set(id, !display.has(id)))
    setError('')
    if (timer.current != null) clearTimeout(timer.current)
    timer.current = setTimeout(() => fireRef.current(), 600)
  }

  const box = (it: ChecklistItemData, label?: string) => {
    const id = String(it.id)
    return (
      <input
        type="checkbox"
        checked={display.has(id)}
        disabled={!interactive}
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
                  className={display.has(String(it.id)) ? 'checked' : ''}
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
                className={`checklist-item${display.has(String(it.id)) ? ' checked' : ''}`}
              >
                {box(it)}
                <span>{it.text}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {submissions > 0 && (
        <span className="checklist-note muted">Отправлено ×{submissions}</span>
      )}
      {error && <div className="auth-error">{error}</div>}
    </div>
  )
}
