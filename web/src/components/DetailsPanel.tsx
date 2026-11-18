import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { useStore } from '@nanostores/react'

import { $user } from '../store/auth'
import {
  $conversations,
  $maxSeq,
  $online,
  $speakerOnline,
  $readSeqs,
  $users,
  addMember,
  convTitle,
  deleteConversation,
  loadUsers,
  patchConversation,
  removeMember,
} from '../store/conversations'
import { $detailsOpen } from '../store/ui'
import { initials } from '../lib/format'
import { IconCheckAll, IconClose, IconEdit, IconTrash } from './icons'

export default function DetailsPanel({ convId }: { convId: number }) {
  const navigate = useNavigate()
  const me = useStore($user)
  const conv = useStore($conversations).find((c) => c.id === convId)
  const users = useStore($users)
  const online = useStore($online)[convId] ?? []
  const speakerOnline = useStore($speakerOnline)
  const readSeqs = useStore($readSeqs)[convId] ?? {}
  const maxSeq = useStore($maxSeq)[convId] ?? 0

  const [editingTitle, setEditingTitle] = useState(false)
  const [title, setTitle] = useState('')
  const [pickUser, setPickUser] = useState('')

  useEffect(() => {
    void loadUsers()
  }, [])

  if (!conv) return null

  const canManage = conv.owner_id === me?.id || me?.permissions['*'] === true
  const archived = conv.status === 'archived'
  const deleted = conv.status === 'deleted'
  const canDelete =
    archived &&
    canManage &&
    (me?.permissions['delete_conversations'] === true ||
      me?.permissions['*'] === true)
  const memberIds = new Set(conv.members.map((m) => m.user_id))
  const candidates = users.filter((u) => !memberIds.has(u.id))

  async function saveTitle() {
    const value = title.trim()
    setEditingTitle(false)
    if (value && value !== conv?.title) {
      await patchConversation(convId, { title: value })
    }
  }

  return (
    <div className="details details-pop">
      <header className="details-head">
        <h2>Детали</h2>
        <button
          className="icon-btn details-close"
          onClick={() => $detailsOpen.set(false)}
          aria-label="Закрыть"
        >
          <IconClose />
        </button>
      </header>

      <div className="details-scroll">
        <section className="details-section">
          {editingTitle ? (
            <input
              className="title-edit"
              value={title}
              autoFocus
              onChange={(e) => setTitle(e.target.value)}
              onBlur={() => void saveTitle()}
              onKeyDown={(e) => e.key === 'Enter' && void saveTitle()}
              maxLength={256}
            />
          ) : (
            <div className="details-title">
              <span>{convTitle(conv)}</span>
              {canManage && (
                <button
                  className="icon-btn"
                  onClick={() => {
                    setTitle(conv.title)
                    setEditingTitle(true)
                  }}
                  aria-label="Переименовать"
                >
                  <IconEdit size={16} />
                </button>
              )}
            </div>
          )}
          <div className="muted small">
            {conv.kind === 'group' ? 'Групповая сессия' : 'Личная сессия'}
            {conv.origin === 'voice_quick' ? ' · голосовая' : ''}
            {archived ? ' · в архиве' : ''}
            {deleted ? ' · удалена' : ''}
          </div>
        </section>

        {canManage && conv.kind === 'group' && (
          <section className="details-section">
            <label className="field">
              <span>Режим ответов</span>
              <select
                value={conv.reply_mode}
                onChange={(e) =>
                  void patchConversation(convId, {
                    reply_mode: e.target.value as 'always' | 'mention',
                  })
                }
              >
                <option value="mention">По упоминанию</option>
                <option value="always">На каждое сообщение</option>
              </select>
            </label>
          </section>
        )}

        <section className="details-section">
          <h3>Участники</h3>
          {conv.kind === 'group' && (
            <div className="member-row">
              <span className="avatar small bot">
                {initials(conv.assistant_name)}
              </span>
              <span className="member-name">
                {conv.assistant_name}
                <span className="muted"> · ассистент</span>
              </span>
            </div>
          )}
          {conv.members.map((m) => (
            <div key={m.user_id} className="member-row">
              <span
                className={`avatar small${online.includes(m.user_id) ? ' online' : ''}${
                  m.username === 'speaker' && speakerOnline ? ' device-online' : ''
                }`}
              >
                {initials(m.display_name)}
              </span>
              <span className="member-name">
                {m.display_name}
                {m.user_id === conv.owner_id && <span className="muted"> · владелец</span>}
              </span>
              {(readSeqs[m.user_id] ?? 0) >= maxSeq && maxSeq > 0 && (
                <IconCheckAll size={14} className="read-mark" />
              )}
              {(canManage || m.user_id === me?.id) && m.user_id !== conv.owner_id && (
                <button
                  className="icon-btn"
                  title={m.user_id === me?.id ? 'Выйти' : 'Удалить'}
                  onClick={() => {
                    void removeMember(convId, m.user_id).then(() => {
                      if (m.user_id === me?.id) navigate('/')
                    })
                  }}
                >
                  <IconTrash size={15} />
                </button>
              )}
            </div>
          ))}
          {canManage && candidates.length > 0 && (
            <div className="member-add">
              <select value={pickUser} onChange={(e) => setPickUser(e.target.value)}>
                <option value="">Добавить участника…</option>
                {candidates.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name}
                  </option>
                ))}
              </select>
              <button
                className="btn"
                disabled={!pickUser}
                onClick={() => {
                  void addMember(convId, Number(pickUser))
                  setPickUser('')
                }}
              >
                Добавить
              </button>
            </div>
          )}
        </section>

        {canManage && !deleted && (
          <section className="details-section">
            <button
              className="btn"
              onClick={() => {
                void patchConversation(convId, {
                  status: archived ? 'active' : 'archived',
                })
                if (!archived) {
                  $detailsOpen.set(false)
                  navigate('/')
                }
              }}
            >
              {archived ? 'Вернуть из архива' : 'В архив'}
            </button>
          </section>
        )}

        {canDelete && (
          <section className="details-section">
            <button
              className="btn"
              onClick={() => {
                if (
                  !window.confirm(
                    'Удалить сессию? Она будет скрыта для всех участников.',
                  )
                )
                  return
                void deleteConversation(convId).then(() => {
                  $detailsOpen.set(false)
                  navigate('/')
                })
              }}
            >
              Удалить сессию
            </button>
          </section>
        )}

        {canManage && deleted && (
          <section className="details-section">
            <button
              className="btn"
              onClick={() =>
                void patchConversation(convId, { status: 'archived' })
              }
            >
              Вернуть в архив
            </button>
          </section>
        )}
      </div>
    </div>
  )
}
