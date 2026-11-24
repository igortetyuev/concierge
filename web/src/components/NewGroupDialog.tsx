import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router'
import { useStore } from '@nanostores/react'

import { createGroup, loadUsers } from '../store/conversations'
import { $users } from '../store/conversations'
import { $user } from '../store/auth'
import { $composerFocus } from '../store/ui'
import { IconClose } from './icons'

export default function NewGroupDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const users = useStore($users)
  const me = useStore($user)
  const [title, setTitle] = useState('')
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void loadUsers()
  }, [])

  function toggle(id: number) {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const conv = await createGroup(title.trim(), [...picked])
      onClose()
      $composerFocus.set(conv.id)
      navigate(`/c/${conv.id}`)
    } catch {
      setError('Не удалось создать группу')
      setBusy(false)
    }
  }

  const others = users.filter((u) => u.id !== me?.id)

  return (
    <div className="modal-scrim" onClick={onClose}>
      <form
        className="modal"
        onClick={(e) => e.stopPropagation()}
        onSubmit={submit}
        aria-label="Новая групповая сессия"
      >
        <div className="modal-head">
          <h2>Новая группа</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть">
            <IconClose />
          </button>
        </div>
        <label className="field">
          <span>Название</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Семья, отпуск, …"
            maxLength={256}
            autoFocus
          />
        </label>
        <div className="member-pick">
          {others.length === 0 && <div className="muted">Загрузка…</div>}
          {others.map((u) => (
            <label key={u.id} className="member-pick-row">
              <input
                type="checkbox"
                checked={picked.has(u.id)}
                onChange={() => toggle(u.id)}
              />
              <span className="avatar">{u.display_name.slice(0, 1).toUpperCase()}</span>
              <span>{u.display_name}</span>
              <span className="muted">@{u.username}</span>
            </label>
          ))}
        </div>
        {error && <div className="auth-error" role="alert">{error}</div>}
        <button className="btn primary" type="submit" disabled={busy || picked.size === 0}>
          {busy ? '…' : 'Создать группу'}
        </button>
      </form>
    </div>
  )
}
