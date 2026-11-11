import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { apiClient } from '../../lib/api'
import type { AuditEventOut, RunOut } from '../../lib/api'
import { fmtDateTime } from '../../lib/format'

const RUN_STATUS: Record<string, string> = {
  queued: 'в очереди',
  running: 'выполняется',
  done: 'готов',
  error: 'ошибка',
}

const ACTION_LABEL: Record<string, string> = {
  'auth.login': 'вход',
  'auth.login.fail': 'неудачный вход',
  'auth.register': 'регистрация',
  'settings.put': 'настройка',
  'settings.delete': 'настройка (удаление)',
  'telegram.link': 'привязка TG',
  'telegram.unlink': 'отвязка TG',
}

function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action.replace(/^admin\./, '')
}

function usageText(usage: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [k, v] of Object.entries(usage)) {
    if (typeof v === 'number' && v > 0) parts.push(`${k}=${v}`)
  }
  return parts.join(' ')
}

export default function AdminJournal() {
  const [runs, setRuns] = useState<RunOut[]>([])
  const [runsTotal, setRunsTotal] = useState(0)
  const [events, setEvents] = useState<AuditEventOut[]>([])
  const [auditTotal, setAuditTotal] = useState(0)
  const [error, setError] = useState('')

  async function loadRuns(offset: number) {
    try {
      const page = await apiClient.listRuns(50, offset)
      setRunsTotal(page.total)
      setRuns((prev) => (offset === 0 ? page.runs : [...prev, ...page.runs]))
    } catch {
      setError('Не удалось загрузить запуски')
    }
  }

  async function loadAudit(offset: number) {
    try {
      const page = await apiClient.listAudit(100, offset)
      setAuditTotal(page.total)
      setEvents((prev) =>
        offset === 0 ? page.events : [...prev, ...page.events],
      )
    } catch {
      setError('Не удалось загрузить аудит')
    }
  }

  useEffect(() => {
    void loadRuns(0)
    void loadAudit(0)
  }, [])

  return (
    <section className="details-section">
      <h3>Запуски агента ({runsTotal})</h3>
      {runs.length === 0 && <div className="muted">Нет запусков</div>}
      {runs.map((r) => (
        <div key={r.id} className="memory-item">
          <span className="memory-item-text">
            #{r.id}{' '}
            <Link to={`/c/${r.conversation_id}`}>
              {r.conversation_title || `#${r.conversation_id}`}
            </Link>
            <span className="muted">
              {' '}
              · {RUN_STATUS[r.status] ?? r.status}
              {r.model ? ` · ${r.model}` : ''}
              {usageText(r.usage) ? ` · ${usageText(r.usage)}` : ''}
            </span>
            {r.error && <span className="auth-error"> {r.error}</span>}
          </span>
        </div>
      ))}
      {runs.length < runsTotal && (
        <button className="btn" onClick={() => void loadRuns(runs.length)}>
          Ещё
        </button>
      )}

      <h3>Аудит ({auditTotal})</h3>
      {events.length === 0 && <div className="muted">Пусто</div>}
      {events.map((e) => (
        <div key={e.id} className="memory-item">
          <span className="memory-item-text">
            <span className="muted">{fmtDateTime(e.at)} · </span>
            {e.actor_name || '—'} · {actionLabel(e.action)}
            {e.target ? ` · ${e.target}` : ''}
            {e.ip ? <span className="muted"> · {e.ip}</span> : ''}
          </span>
        </div>
      ))}
      {events.length < auditTotal && (
        <button className="btn" onClick={() => void loadAudit(events.length)}>
          Ещё
        </button>
      )}
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
