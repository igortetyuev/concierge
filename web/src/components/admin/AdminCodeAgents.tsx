import { useEffect, useState } from 'react'

import { apiClient } from '../../lib/api'
import type { CodeAgentOut, CodeAgentProject } from '../../lib/api'
import { IconTrash } from '../icons'

const EMPTY_PROJECT: CodeAgentProject = { name: '', path: '', description: '' }

export default function AdminCodeAgents() {
  const [agents, setAgents] = useState<CodeAgentOut[] | null>(null)
  const [projects, setProjects] = useState<CodeAgentProject[]>([])
  const [newName, setNewName] = useState('devin')
  const [newHost, setNewHost] = useState('mac')
  const [newWorkdir, setNewWorkdir] = useState('')
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [rows, gs] = await Promise.all([
        apiClient.listCodeAgents(),
        apiClient.listGlobalSettings(),
      ])
      setAgents(rows)
      const p = gs['code_agent.projects']
      setProjects(Array.isArray(p) ? (p as CodeAgentProject[]) : [])
    } catch {
      setError('Не удалось загрузить настройки')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function setDefault(id: number) {
    try {
      await apiClient.patchCodeAgent(id, { is_default: true })
      await reload()
    } catch {
      setError('Не удалось сохранить')
    }
  }

  async function addAgent() {
    if (!newWorkdir.trim()) return
    try {
      await apiClient.createCodeAgent({
        name: newName,
        ssh_host: newHost.trim() || 'mac',
        workdir: newWorkdir.trim(),
      })
      setNewWorkdir('')
      await reload()
    } catch {
      setError('Не удалось добавить агента')
    }
  }

  async function removeAgent(id: number) {
    await apiClient.deleteCodeAgent(id).catch(() => undefined)
    await reload()
  }

  function patchProject(i: number, patch: Partial<CodeAgentProject>) {
    setProjects((rows) =>
      rows.map((r, j) => (j === i ? { ...r, ...patch } : r)),
    )
  }

  async function saveProjects() {
    const rows = projects.filter((p) => p.name.trim() && p.path.trim())
    try {
      await apiClient.putGlobalSetting('code_agent.projects', rows)
      setProjects(rows)
    } catch {
      setError('Не удалось сохранить проекты')
    }
  }

  return (
    <section className="details-section">
      <h3>Код-агенты</h3>
      {agents === null ? (
        <div className="muted">Загрузка…</div>
      ) : (
        <>
          {agents.length === 0 && <div className="muted">Нет агентов</div>}
          {agents.map((a) => (
            <div key={a.id} className="memory-item">
              <label className="memory-item-text">
                <input
                  type="radio"
                  name="default-code-agent"
                  checked={a.is_default}
                  onChange={() => void setDefault(a.id)}
                />{' '}
                {a.name} · {a.ssh_host}:{a.workdir}
                <span className="muted">
                  {a.is_default ? ' · по умолчанию' : ''}
                </span>
              </label>
              <button
                className="icon-btn"
                title="Удалить агента"
                aria-label="Удалить агента"
                onClick={() => void removeAgent(a.id)}
              >
                <IconTrash size={16} />
              </button>
            </div>
          ))}
          <div className="settings-form">
            <select value={newName} onChange={(e) => setNewName(e.target.value)}>
              <option value="devin">devin</option>
              <option value="pi">pi</option>
            </select>
            <input
              value={newHost}
              onChange={(e) => setNewHost(e.target.value)}
              placeholder="ssh host"
            />
            <input
              value={newWorkdir}
              onChange={(e) => setNewWorkdir(e.target.value)}
              placeholder="рабочая директория (напр. ~/projects)"
            />
            <button
              className="btn"
              onClick={() => void addAgent()}
              disabled={!newWorkdir.trim()}
            >
              Добавить
            </button>
          </div>
        </>
      )}

      <h3>Проекты</h3>
      {projects.map((p, i) => (
        <div key={i} className="settings-form">
          <input
            value={p.name}
            onChange={(e) => patchProject(i, { name: e.target.value })}
            placeholder="имя"
          />
          <input
            value={p.path}
            onChange={(e) => patchProject(i, { path: e.target.value })}
            placeholder="путь на Mac"
          />
          <input
            value={p.description ?? ''}
            onChange={(e) => patchProject(i, { description: e.target.value })}
            placeholder="описание (необязательно)"
          />
          <button
            className="icon-btn"
            title="Удалить проект"
            aria-label="Удалить проект"
            onClick={() =>
              setProjects((rows) => rows.filter((_, j) => j !== i))
            }
          >
            <IconTrash size={16} />
          </button>
        </div>
      ))}
      <div className="settings-row">
        <button
          className="btn"
          onClick={() => setProjects((rows) => [...rows, { ...EMPTY_PROJECT }])}
        >
          Добавить проект
        </button>
        <button className="btn primary" onClick={() => void saveProjects()}>
          Сохранить проекты
        </button>
      </div>

      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
