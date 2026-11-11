import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { apiClient } from '../../lib/api'
import type {
  AdminTelegramLinkOut,
  AdminUserOut,
  TelegramOverviewOut,
} from '../../lib/api'
import { IconTrash } from '../icons'

function isMaskedSecret(value: unknown): boolean {
  return (
    typeof value === 'object' &&
    value !== null &&
    'secret' in (value as Record<string, unknown>)
  )
}

const KIND_LABEL: Record<string, string> = {
  dm_current: 'личка',
  topic: 'топик',
  group: 'группа',
}

export default function AdminTelegram() {
  const [overview, setOverview] = useState<TelegramOverviewOut | null>(null)
  const [links, setLinks] = useState<AdminTelegramLinkOut[]>([])
  const [users, setUsers] = useState<AdminUserOut[]>([])
  const [enabled, setEnabled] = useState(false)
  const [hasToken, setHasToken] = useState(false)
  const [token, setToken] = useState('')
  const [allowed, setAllowed] = useState('')
  const [newUser, setNewUser] = useState('')
  const [newTgId, setNewTgId] = useState('')
  const [newTgName, setNewTgName] = useState('')
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [ov, ls, gs, us] = await Promise.all([
        apiClient.adminTelegramOverview(),
        apiClient.adminTelegramLinks(),
        apiClient.listGlobalSettings(),
        apiClient.listAdminUsers(),
      ])
      setOverview(ov)
      setLinks(ls)
      setUsers(us)
      setEnabled(gs['telegram.enabled'] === true)
      setHasToken(isMaskedSecret(gs['telegram.bot_token']))
      const ids = gs['telegram.allowed_user_ids']
      setAllowed(Array.isArray(ids) ? ids.join(', ') : '')
    } catch {
      setError('Не удалось загрузить настройки')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function toggleEnabled(value: boolean) {
    setEnabled(value)
    await apiClient
      .putGlobalSetting('telegram.enabled', value)
      .catch(() => setError('Не удалось сохранить'))
  }

  async function saveToken() {
    const value = token.trim()
    if (!value) return
    try {
      await apiClient.putGlobalSetting('telegram.bot_token', { secret: value })
      setToken('')
      setHasToken(true)
    } catch {
      setError('Не удалось сохранить токен')
    }
  }

  async function saveAllowed() {
    const ids = allowed
      .split(',')
      .map((s) => parseInt(s.trim(), 10))
      .filter((n) => Number.isFinite(n))
    try {
      await apiClient.putGlobalSetting('telegram.allowed_user_ids', ids)
    } catch {
      setError('Не удалось сохранить allowlist')
    }
  }

  async function addLink() {
    const tgId = parseInt(newTgId.trim(), 10)
    const userId = parseInt(newUser, 10)
    if (!Number.isFinite(tgId) || !Number.isFinite(userId)) return
    try {
      await apiClient.adminTelegramAddLink(userId, tgId, newTgName.trim() || undefined)
      setNewTgId('')
      setNewTgName('')
      await reload()
    } catch {
      setError('Не удалось добавить привязку')
    }
  }

  async function removeLink(userId: number) {
    await apiClient.adminTelegramRemoveLink(userId).catch(() => undefined)
    await reload()
  }

  return (
    <section className="details-section">
      <h3>Бот</h3>
      <div className="settings-row">
        <span>
          {enabled ? 'включён' : 'выключен'}
          {overview && (
            <span className="muted">
              {' · '}
              {overview.running ? 'работает' : 'остановлен'}
              {overview.bot_username ? ` · @${overview.bot_username}` : ''}
            </span>
          )}
        </span>
        <label className="settings-toggle">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => void toggleEnabled(e.target.checked)}
          />
          Включён
        </label>
      </div>

      <label className="field">
        <span>Токен бота</span>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={hasToken ? 'сохранён' : 'токен от @BotFather'}
          autoComplete="off"
        />
      </label>
      {token.trim() && (
        <button className="btn" onClick={() => void saveToken()}>
          Сохранить токен
        </button>
      )}

      <label className="field">
        <span>Разрешённые telegram user id (через запятую, пусто — все)</span>
        <input
          value={allowed}
          onChange={(e) => setAllowed(e.target.value)}
          onBlur={() => void saveAllowed()}
          placeholder="123456789, 987654321"
          inputMode="numeric"
        />
      </label>

      <h3>Привязки</h3>
      {links.length === 0 && <div className="muted">Нет привязок</div>}
      {links.map((l) => (
        <div key={l.user_id} className="memory-item">
          <span className="memory-item-text">
            {l.username} → {l.telegram_user_id}
            {l.telegram_username ? ` (@${l.telegram_username})` : ''}
          </span>
          <button
            className="icon-btn"
            title="Удалить привязку"
            aria-label="Удалить привязку"
            onClick={() => void removeLink(l.user_id)}
          >
            <IconTrash size={16} />
          </button>
        </div>
      ))}
      <div className="settings-form">
        <select value={newUser} onChange={(e) => setNewUser(e.target.value)}>
          <option value="">пользователь…</option>
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.display_name} ({u.username})
            </option>
          ))}
        </select>
        <input
          value={newTgId}
          onChange={(e) => setNewTgId(e.target.value)}
          placeholder="telegram user id"
          inputMode="numeric"
        />
        <input
          value={newTgName}
          onChange={(e) => setNewTgName(e.target.value)}
          placeholder="@username (необязательно)"
        />
        <button
          className="btn"
          onClick={() => void addLink()}
          disabled={!newUser || !newTgId.trim()}
        >
          Добавить
        </button>
      </div>

      {overview && overview.bindings.length > 0 && (
        <>
          <h3>Чаты</h3>
          {overview.bindings.map((b) => (
            <div key={`${b.chat_id}:${b.thread_id}`} className="memory-item">
              <span className="memory-item-text">
                {KIND_LABEL[b.kind] ?? b.kind} · {b.chat_id}
                {b.thread_id ? `/${b.thread_id}` : ''} →{' '}
                <Link to={`/c/${b.conversation_id}`}>
                  {b.title || `#${b.conversation_id}`}
                </Link>
              </span>
            </div>
          ))}
        </>
      )}
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
