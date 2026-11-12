import { useEffect, useState } from 'react'

import { apiClient } from '../../lib/api'
import type { RoleOut } from '../../lib/api'
import { IconTrash } from '../icons'

const BOOL_PERMS: { key: string; label: string }[] = [
  { key: '*', label: 'Суперадмин (*)' },
  { key: 'manage_users', label: 'Пользователи и роли' },
  { key: 'create_invites', label: 'Приглашения' },
  { key: 'manage_providers', label: 'Провайдеры и модели' },
  { key: 'manage_mcp', label: 'MCP-серверы' },
  { key: 'manage_code_agents', label: 'Код-агенты' },
  { key: 'telegram', label: 'Telegram' },
  { key: 'code_agent', label: 'Делегирование кода' },
  { key: 'voice_quick_session', label: 'Голосовая быстрая сессия' },
  { key: 'family_memory', label: 'Семейная память (запись)' },
  { key: 'delete_conversations', label: 'Удаление сессий' },
]

const SETTING_SCOPES = ['user', 'role', 'global'] as const
const SETTING_LEVELS = ['none', 'r', 'rw'] as const

type Perms = Record<string, unknown>

function scopeLevel(perms: Perms, scope: string): string {
  const conf = perms['settings']
  if (typeof conf === 'string') return scope === 'user' ? conf : 'none'
  if (typeof conf === 'object' && conf !== null) {
    return String((conf as Perms)[scope] ?? 'none')
  }
  return 'none'
}

function setScopeLevel(perms: Perms, scope: string, level: string): Perms {
  const conf = perms['settings']
  const base: Perms =
    typeof conf === 'object' && conf !== null
      ? { ...(conf as Perms) }
      : typeof conf === 'string'
        ? { user: conf }
        : {}
  base[scope] = level
  return { ...perms, settings: base }
}

function listValue(perms: Perms, key: string): string {
  const v = perms[key]
  return Array.isArray(v) ? v.join(', ') : ''
}

interface Draft {
  name: string
  prompt_fragment: string
  perms: Perms
}

export default function AdminRoles() {
  const [roles, setRoles] = useState<RoleOut[] | null>(null)
  const [editId, setEditId] = useState<number | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [newName, setNewName] = useState('')
  const [error, setError] = useState('')

  async function reload() {
    try {
      setRoles(await apiClient.listRoles())
    } catch {
      setError('Не удалось загрузить роли')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  function startEdit(r: RoleOut) {
    setEditId(r.id)
    setDraft({
      name: r.name,
      prompt_fragment: r.prompt_fragment ?? '',
      perms: { ...r.permissions },
    })
  }

  function patchPerms(p: Perms) {
    if (draft) setDraft({ ...draft, perms: p })
  }

  async function saveEdit() {
    if (editId === null || draft === null) return
    try {
      await apiClient.patchRole(editId, {
        name: draft.name,
        prompt_fragment: draft.prompt_fragment || null,
        permissions: draft.perms,
      })
      setEditId(null)
      setDraft(null)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось сохранить')
    }
  }

  async function createRole() {
    if (!newName.trim()) return
    try {
      await apiClient.createRole({
        name: newName.trim(),
        permissions: { settings: { user: 'rw' } },
      })
      setNewName('')
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось создать роль')
    }
  }

  async function removeRole(r: RoleOut) {
    if (!window.confirm(`Удалить роль ${r.name}?`)) return
    try {
      await apiClient.deleteRole(r.id)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить роль')
    }
  }

  function permBool(key: string): boolean {
    return draft?.perms[key] === true
  }

  function toolList(which: 'allow' | 'deny'): string {
    const tools = draft?.perms['tools']
    if (typeof tools === 'object' && tools !== null) {
      const v = (tools as Perms)[which]
      return Array.isArray(v) ? v.join(', ') : ''
    }
    return ''
  }

  function setToolList(which: 'allow' | 'deny', raw: string) {
    if (!draft) return
    const tools =
      typeof draft.perms['tools'] === 'object' && draft.perms['tools'] !== null
        ? { ...(draft.perms['tools'] as Perms) }
        : {}
    tools[which] = raw
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    patchPerms({ ...draft.perms, tools })
  }

  return (
    <section className="details-section">
      <h3>Роли и права</h3>
      {roles === null ? (
        <div className="muted">Загрузка…</div>
      ) : (
        roles.map((r) => (
          <div key={r.id}>
            <div className="memory-item">
              <span className="memory-item-text">
                {r.name}
                <span className="muted">
                  {' '}
                  · {r.permissions['*'] === true ? 'полный доступ' : ''}
                </span>
              </span>
              <button
                className="btn"
                onClick={() =>
                  editId === r.id ? setEditId(null) : startEdit(r)
                }
              >
                {editId === r.id ? 'Скрыть' : 'Изменить'}
              </button>
              <button
                className="icon-btn"
                title="Удалить роль"
                aria-label="Удалить роль"
                onClick={() => void removeRole(r)}
              >
                <IconTrash size={16} />
              </button>
            </div>
            {editId === r.id && draft !== null && (
              <div className="settings-form admin-edit">
                <input
                  value={draft.name}
                  onChange={(e) =>
                    setDraft({ ...draft, name: e.target.value })
                  }
                  placeholder="имя роли"
                />
                <textarea
                  value={draft.prompt_fragment}
                  onChange={(e) =>
                    setDraft({ ...draft, prompt_fragment: e.target.value })
                  }
                  placeholder="фрагмент промпта роли"
                  rows={2}
                />
                <div className="admin-perms">
                  {BOOL_PERMS.map((p) => (
                    <label key={p.key} className="settings-toggle">
                      <input
                        type="checkbox"
                        checked={permBool(p.key)}
                        onChange={(e) =>
                          patchPerms({
                            ...draft.perms,
                            [p.key]: e.target.checked,
                          })
                        }
                      />
                      {p.label}
                    </label>
                  ))}
                </div>
                <div className="settings-form">
                  {SETTING_SCOPES.map((scope) => (
                    <label key={scope} className="field">
                      <span>Настройки: {scope}</span>
                      <select
                        value={scopeLevel(draft.perms, scope)}
                        onChange={(e) =>
                          patchPerms(
                            setScopeLevel(draft.perms, scope, e.target.value),
                          )
                        }
                      >
                        {SETTING_LEVELS.map((l) => (
                          <option key={l} value={l}>
                            {l}
                          </option>
                        ))}
                      </select>
                    </label>
                  ))}
                  <label className="field">
                    <span>Лимит загрузок, МБ</span>
                    <input
                      type="number"
                      min={0}
                      value={Number(draft.perms['max_upload_mb'] ?? 0)}
                      onChange={(e) =>
                        patchPerms({
                          ...draft.perms,
                          max_upload_mb: Number(e.target.value),
                        })
                      }
                    />
                  </label>
                  <label className="field">
                    <span>Инструменты: allow</span>
                    <input
                      value={toolList('allow')}
                      onChange={(e) => setToolList('allow', e.target.value)}
                      placeholder="* или список через запятую"
                    />
                  </label>
                  <label className="field">
                    <span>Инструменты: deny</span>
                    <input
                      value={toolList('deny')}
                      onChange={(e) => setToolList('deny', e.target.value)}
                      placeholder="список через запятую"
                    />
                  </label>
                  <label className="field">
                    <span>MCP-серверы (имена через запятую)</span>
                    <input
                      value={listValue(draft.perms, 'mcp_servers')}
                      onChange={(e) =>
                        patchPerms({
                          ...draft.perms,
                          mcp_servers: e.target.value
                            .split(',')
                            .map((s) => s.trim())
                            .filter(Boolean),
                        })
                      }
                      placeholder="* разрешены все"
                    />
                  </label>
                </div>
                <div className="settings-row">
                  <button className="btn primary" onClick={() => void saveEdit()}>
                    Сохранить
                  </button>
                </div>
              </div>
            )}
          </div>
        ))
      )}

      <div className="settings-form">
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="имя новой роли"
        />
        <button
          className="btn"
          onClick={() => void createRole()}
          disabled={!newName.trim()}
        >
          Создать роль
        </button>
      </div>
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
