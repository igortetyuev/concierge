import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import type {
  ScheduleOut,
  TelegramLinkCodeOut,
  TelegramLinkOut,
} from '../lib/api'
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

function fmtLocal(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString('ru-RU', {
        day: '2-digit',
        month: '2-digit',
        year: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
}

function SchedulesSection() {
  const [jobs, setJobs] = useState<ScheduleOut[] | null>(null)
  const [error, setError] = useState('')
  const [pauseFor, setPauseFor] = useState<number | null>(null)
  const [pauseValue, setPauseValue] = useState('')

  async function reload() {
    try {
      setJobs(await apiClient.listSchedules())
    } catch {
      setJobs([])
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function remove(id: number) {
    if (!confirm('Удалить паттерн из планировщика?')) return
    try {
      await apiClient.deleteSchedule(id)
      await reload()
    } catch {
      setError('Не удалось удалить')
    }
  }

  async function resume(id: number) {
    try {
      await apiClient.patchSchedule(id, null)
      await reload()
    } catch {
      setError('Не удалось возобновить')
    }
  }

  async function pause(id: number) {
    if (!pauseValue) return
    try {
      await apiClient.patchSchedule(id, new Date(pauseValue).toISOString())
      setPauseFor(null)
      setPauseValue('')
      await reload()
    } catch {
      setError('Не удалось приостановить')
    }
  }

  return (
    <section className="details-section">
      <h3>Расписание</h3>
      {jobs === null ? (
        <div className="muted">Загрузка…</div>
      ) : jobs.length === 0 ? (
        <div className="muted">
          Нет активных паттернов. Попросите консьержа: «напомни через 10 минут
          …» или «каждый день в 8:00 …».
        </div>
      ) : (
        jobs.map((j) => (
          <div key={j.id} className="schedule-item">
            <div className="settings-row">
              <span>
                {j.kind === 'reminder' ? '⏰ ' : '📅 '}
                <b>{j.title || j.text}</b>
                {j.title && j.text !== j.title ? ` — ${j.text}` : ''}
              </span>
            </div>
            <div className="settings-row muted">
              <span>
                {j.schedule}
                {j.next_runs[0] ? ` · следующее: ${fmtLocal(j.next_runs[0])}` : ''}
                {j.paused_until
                  ? ` · пауза до ${fmtLocal(`${j.paused_until}Z`)}`
                  : ''}
              </span>
            </div>
            <div className="settings-row">
              {j.paused_until ? (
                <button className="btn" onClick={() => void resume(j.id)}>
                  Возобновить
                </button>
              ) : pauseFor === j.id ? (
                <span className="settings-row">
                  <input
                    type="datetime-local"
                    value={pauseValue}
                    onChange={(e) => setPauseValue(e.target.value)}
                  />
                  <button className="btn" onClick={() => void pause(j.id)}>
                    Ок
                  </button>
                  <button className="btn" onClick={() => setPauseFor(null)}>
                    Отмена
                  </button>
                </span>
              ) : (
                <button className="btn" onClick={() => setPauseFor(j.id)}>
                  Пауза…
                </button>
              )}
              {j.conversation_id !== null && (
                <Link className="btn" to={`/c/${j.conversation_id}`}>
                  Сессия
                </Link>
              )}
              <button className="btn" onClick={() => void remove(j.id)}>
                Удалить
              </button>
            </div>
          </div>
        ))
      )}
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
          <div className="chat-sub muted">Telegram · Расписание</div>
        </div>
      </header>

      <div className="details-scroll">
        {canTelegram && <TelegramSection />}
        <SchedulesSection />
      </div>
    </div>
  )
}
