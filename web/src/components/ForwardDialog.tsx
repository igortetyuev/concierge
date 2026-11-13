import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import { $users, loadUsers, refreshConversation } from '../store/conversations'
import { $user } from '../store/auth'
import { IconClose } from './icons'

export default function ForwardDialog({
  convId,
  messageIds,
  excludeUserIds,
  onClose,
  onDone,
}: {
  convId: number
  messageIds: number[]
  excludeUserIds: Set<number>
  onClose: () => void
  onDone: (targetConvId: number) => void
}) {
  const users = useStore($users)
  const me = useStore($user)
  const [picked, setPicked] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void loadUsers()
  }, [])

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (picked == null) return
    setBusy(true)
    setError('')
    try {
      const res = await apiClient.forwardMessages(convId, picked, messageIds)
      await refreshConversation(res.conversation_id)
      onDone(res.conversation_id)
    } catch {
      setError('Не удалось переслать')
      setBusy(false)
    }
  }

  const targets = users.filter(
    (u) => u.id !== me?.id && !excludeUserIds.has(u.id),
  )

  return (
    <div className="modal-scrim" onClick={onClose}>
      <form
        className="modal"
        onClick={(e) => e.stopPropagation()}
        onSubmit={submit}
        aria-label="Переслать сообщения"
      >
        <div className="modal-head">
          <h2>Переслать {messageIds.length} сообщ.</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть">
            <IconClose />
          </button>
        </div>
        <div className="member-pick">
          {targets.length === 0 && <div className="muted">Некому переслать</div>}
          {targets.map((u) => (
            <label key={u.id} className="member-pick-row">
              <input
                type="radio"
                name="forward-target"
                checked={picked === u.id}
                onChange={() => setPicked(u.id)}
              />
              <span className="avatar">{u.display_name.slice(0, 1).toUpperCase()}</span>
              <span>{u.display_name}</span>
              <span className="muted">@{u.username}</span>
            </label>
          ))}
        </div>
        {error && <div className="auth-error" role="alert">{error}</div>}
        <button className="btn primary" type="submit" disabled={busy || picked == null}>
          {busy ? '…' : 'Переслать'}
        </button>
      </form>
    </div>
  )
}
