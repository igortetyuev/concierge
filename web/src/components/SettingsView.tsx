import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import type { TelegramLinkCodeOut, TelegramLinkOut } from '../lib/api'
import { $user } from '../store/auth'
import { IconBack } from './icons'

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

export default function SettingsView() {
  const me = useStore($user)
  const canTelegram =
    me?.permissions['*'] === true ||
    me?.permissions['manage_users'] === true ||
    me?.permissions['telegram'] === true

  return (
    <div className="memory">
      <header className="chat-head">
        <Link to="/" className="icon-btn chat-back" aria-label="Назад">
          <IconBack />
        </Link>
        <div className="chat-head-text">
          <div className="chat-title">Настройки</div>
          <div className="chat-sub muted">Telegram</div>
        </div>
      </header>

      <div className="details-scroll">
        {canTelegram && <TelegramSection />}
      </div>
    </div>
  )
}
