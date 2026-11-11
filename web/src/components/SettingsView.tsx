import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import type {
  AdminTelegramLinkOut,
  AdminUserOut,
  TelegramLinkCodeOut,
  TelegramLinkOut,
  TelegramOverviewOut,
} from '../lib/api'
import { $user } from '../store/auth'
import { IconBack, IconTrash } from './icons'

function isMaskedSecret(value: unknown): boolean {
  return (
    typeof value === 'object' &&
    value !== null &&
    'secret' in (value as Record<string, unknown>)
  )
}

function TelegramSection() {
  const [link, setLink] = useState<TelegramLinkOut | null>(null)
  const [code, setCode] = useState<TelegramLinkCodeOut | null>(null)
  const [voice, setVoice] = useState('off')
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [l, settings] = await Promise.all([
        apiClient.telegramLink(),
        apiClient.listUserSettings(),
      ])
      setLink(l)
      setVoice(String(settings['telegram.voice_reply'] ?? 'off'))
    } catch {
      setLink({ linked: false, telegram_username: null, telegram_user_id: null, bot_username: null })
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function makeCode() {
    setError('')
    try {
      setCode(await apiClient.telegramLinkCode())
    } catch {
      setError('Не удалось создать код')
    }
  }

  async function unlink() {
    try {
      await apiClient.telegramUnlink()
      setCode(null)
      await reload()
    } catch {
      setError('Не удалось отвязать')
    }
  }

  async function changeVoice(value: string) {
    setVoice(value)
    try {
      await apiClient.putUserSetting('telegram.voice_reply', value)
    } catch {
      setError('Не удалось сохранить настройку')
    }
  }

  const expires = code
    ? new Date(`${code.expires_at}Z`).toLocaleTimeString('ru-RU', {
        hour: '2-digit',
        minute: '2-digit',
      })
    : ''

  return (
    <section className="details-section">
      <h3>Telegram</h3>
      {link === null ? (
        <div className="muted">Загрузка…</div>
      ) : link.linked ? (
        <div className="settings-row">
          <span>
            Привязан:{' '}
            {link.telegram_username
              ? `@${link.telegram_username}`
              : `#${link.telegram_user_id}`}
          </span>
          <button className="btn" onClick={() => void unlink()}>
            Отвязать
          </button>
        </div>
      ) : code ? (
        <div className="link-block">
          <div className="link-code">{code.code}</div>
          <div className="muted">
            Откройте бота{code.bot_username ? ` @${code.bot_username}` : ''} и
            отправьте <code>/start {code.code}</code>
          </div>
          <div className="muted">Код действует до {expires}</div>
          {code.bot_username && (
            <a
              className="btn"
              href={`https://t.me/${code.bot_username}?start=${code.code}`}
              target="_blank"
              rel="noreferrer"
            >
              Открыть бота
            </a>
          )}
        </div>
      ) : (
        <button className="btn primary" onClick={() => void makeCode()}>
          Привязать Telegram
        </button>
      )}
      <label className="field">
        <span>Голосовые ответы</span>
        <select value={voice} onChange={(e) => void changeVoice(e.target.value)}>
          <option value="off">выключены</option>
          <option value="on-request">в ответ на голосовое</option>
          <option value="always">всегда</option>
        </select>
      </label>
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}

const KIND_LABEL: Record<string, string> = {
  dm_current: 'личка',
  topic: 'топик',
  group: 'группа',
}

function AdminTelegramSection() {
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
      <h3>Администрирование</h3>
      <div className="settings-row">
        <span>
          Бот: {enabled ? 'включён' : 'выключен'}
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

export default function SettingsView() {
  const me = useStore($user)
  const isAdmin =
    me?.permissions['*'] === true || me?.permissions['manage_users'] === true
  const canTelegram = isAdmin || me?.permissions['telegram'] === true

  return (
    <div className="memory">
      <header className="chat-head">
        <Link to="/" className="icon-btn chat-back" aria-label="Назад">
          <IconBack />
        </Link>
        <div className="chat-head-text">
          <div className="chat-title">Настройки</div>
          <div className="chat-sub muted">Telegram и администрирование</div>
        </div>
      </header>

      <div className="details-scroll">
        {canTelegram && <TelegramSection />}
        {isAdmin && <AdminTelegramSection />}
      </div>
    </div>
  )
}
