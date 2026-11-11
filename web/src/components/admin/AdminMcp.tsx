import { useEffect, useState } from 'react'

import { apiClient } from '../../lib/api'
import type { McpServerOut } from '../../lib/api'
import { IconTrash } from '../icons'

const TRANSPORTS = ['stdio', 'streamable_http', 'http', 'sse']

const EMPTY = {
  name: '',
  transport: 'stdio',
  command: '',
  url: '',
  env_ref: '',
  allowed_tools: '',
}

export default function AdminMcp() {
  const [servers, setServers] = useState<McpServerOut[] | null>(null)
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState('')

  async function reload() {
    try {
      setServers(await apiClient.listMcpServers())
    } catch {
      setError('Не удалось загрузить MCP-серверы')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function toggle(s: McpServerOut) {
    try {
      await apiClient.patchMcpServer(s.id, { enabled: !s.enabled })
      await reload()
    } catch {
      setError('Не удалось сохранить')
    }
  }

  async function create() {
    if (!form.name.trim()) return
    try {
      await apiClient.createMcpServer({
        name: form.name.trim(),
        transport: form.transport,
        command: form.command.trim() || undefined,
        url: form.url.trim() || undefined,
        env_ref: form.env_ref.trim() || undefined,
        allowed_tools: form.allowed_tools
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
      })
      setForm(EMPTY)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось создать')
    }
  }

  async function remove(id: number) {
    try {
      await apiClient.deleteMcpServer(id)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить')
    }
  }

  return (
    <section className="details-section">
      <h3>MCP-серверы</h3>
      {servers === null ? (
        <div className="muted">Загрузка…</div>
      ) : (
        <>
          {servers.length === 0 && <div className="muted">Нет серверов</div>}
          {servers.map((s) => (
            <div key={s.id} className="memory-item">
              <span className="memory-item-text">
                {s.name} <span className="muted">· {s.transport}</span>
                <span className="muted">
                  {' '}
                  · {s.command ?? s.url ?? ''}
                  {!s.enabled && ' · выключен'}
                </span>
              </span>
              <label className="settings-toggle">
                <input
                  type="checkbox"
                  checked={s.enabled}
                  onChange={() => void toggle(s)}
                />
              </label>
              <button
                className="icon-btn"
                title="Удалить сервер"
                aria-label="Удалить сервер"
                onClick={() => void remove(s.id)}
              >
                <IconTrash size={16} />
              </button>
            </div>
          ))}
        </>
      )}
      <div className="settings-form">
        <input
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder="имя"
        />
        <select
          value={form.transport}
          onChange={(e) => setForm({ ...form, transport: e.target.value })}
        >
          {TRANSPORTS.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        {form.transport === 'stdio' ? (
          <input
            value={form.command}
            onChange={(e) => setForm({ ...form, command: e.target.value })}
            placeholder="команда (stdio)"
          />
        ) : (
          <input
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.target.value })}
            placeholder="url"
          />
        )}
        <input
          value={form.env_ref}
          onChange={(e) => setForm({ ...form, env_ref: e.target.value })}
          placeholder="env_ref (настройка/env с переменными)"
        />
        <input
          value={form.allowed_tools}
          onChange={(e) =>
            setForm({ ...form, allowed_tools: e.target.value })
          }
          placeholder="allowed_tools через запятую (пусто — все)"
        />
        <button
          className="btn"
          onClick={() => void create()}
          disabled={!form.name.trim()}
        >
          Добавить сервер
        </button>
      </div>
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
