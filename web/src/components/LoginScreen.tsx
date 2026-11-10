import { useState } from 'react'
import type { FormEvent } from 'react'

import { ApiError } from '../lib/api'
import { login, register } from '../store/auth'

function extractToken(raw: string): string {
  const trimmed = raw.trim()
  try {
    return new URL(trimmed).searchParams.get('token') ?? trimmed
  } catch {
    return trimmed
  }
}

export default function LoginScreen() {
  const params = new URLSearchParams(window.location.search)
  const [mode, setMode] = useState<'login' | 'register'>(
    params.get('token') ? 'register' : 'login',
  )
  const [invite, setInvite] = useState(params.get('token') ?? '')
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (mode === 'login') {
        await login(username.trim(), password)
      } else {
        await register(extractToken(invite), username.trim(), displayName.trim(), password)
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 401
            ? 'Неверный логин или пароль'
            : err.status === 400
              ? 'Ссылка-приглашение недействительна или уже использована'
              : `Ошибка: ${err.message}`,
        )
      } else {
        setError('Нет связи с сервером')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth">
      <form className="auth-card" onSubmit={submit}>
        <h1 className="auth-title">Консьерж</h1>
        <div className="auth-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'login'}
            className={mode === 'login' ? 'active' : ''}
            onClick={() => setMode('login')}
          >
            Вход
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'register'}
            className={mode === 'register' ? 'active' : ''}
            onClick={() => setMode('register')}
          >
            По приглашению
          </button>
        </div>

        {mode === 'register' && (
          <>
            <label className="field">
              <span>Приглашение (ссылка или код)</span>
              <input
                value={invite}
                onChange={(e) => setInvite(e.target.value)}
                required
                autoComplete="off"
                placeholder="https://…?token=…"
              />
            </label>
            <label className="field">
              <span>Имя</span>
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                required
                autoComplete="name"
                placeholder="Как вас называть"
              />
            </label>
          </>
        )}

        <label className="field">
          <span>Логин</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            minLength={mode === 'register' ? 3 : 1}
            autoComplete="username"
          />
        </label>
        <label className="field">
          <span>Пароль</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={mode === 'register' ? 8 : 1}
            autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
          />
        </label>

        {error && <div className="auth-error" role="alert">{error}</div>}

        <button className="btn primary" type="submit" disabled={busy}>
          {busy ? '…' : mode === 'login' ? 'Войти' : 'Создать аккаунт'}
        </button>
      </form>
    </div>
  )
}
