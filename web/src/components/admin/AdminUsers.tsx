import { useEffect, useState } from 'react'
import { useStore } from '@nanostores/react'

import { apiClient } from '../../lib/api'
import type { AdminUserOut, InviteOut, RoleOut } from '../../lib/api'
import { $user } from '../../store/auth'
import { IconTrash } from '../icons'

const EMPTY_FORM = {
  username: '',
  display_name: '',
  password: '',
  role_id: 0,
}

interface EditDraft {
  display_name: string
  role_id: number
  is_active: boolean
  password: string
  prompt_fragment: string
}

export default function AdminUsers() {
  const me = useStore($user)
  const [users, setUsers] = useState<AdminUserOut[] | null>(null)
  const [roles, setRoles] = useState<RoleOut[]>([])
  const [invites, setInvites] = useState<InviteOut[]>([])
  const [inviteRole, setInviteRole] = useState('')
  const [inviteLink, setInviteLink] = useState('')
  const [form, setForm] = useState(EMPTY_FORM)
  const [editId, setEditId] = useState<number | null>(null)
  const [draft, setDraft] = useState<EditDraft | null>(null)
  const [error, setError] = useState('')

  const canInvite =
    me?.permissions['*'] === true || me?.permissions['create_invites'] === true

  async function reload() {
    try {
      const [us, rs] = await Promise.all([
        apiClient.listAdminUsers(),
        apiClient.listRoles(),
      ])
      setUsers(us)
      setRoles(rs)
      if (canInvite) setInvites(await apiClient.listInvites())
    } catch {
      setError('Не удалось загрузить данные')
    }
  }

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function startEdit(u: AdminUserOut) {
    setEditId(u.id)
    setDraft({
      display_name: u.display_name,
      role_id: u.role_id,
      is_active: u.is_active,
      password: '',
      prompt_fragment: u.prompt_fragment ?? '',
    })
  }

  async function saveEdit() {
    if (editId === null || draft === null) return
    try {
      await apiClient.patchAdminUser(editId, {
        display_name: draft.display_name,
        role_id: draft.role_id,
        is_active: draft.is_active,
        prompt_fragment: draft.prompt_fragment || null,
        ...(draft.password ? { password: draft.password } : {}),
      })
      setEditId(null)
      setDraft(null)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось сохранить')
    }
  }

  async function createUser() {
    if (!form.username.trim() || !form.display_name.trim() || !form.role_id)
      return
    try {
      await apiClient.createAdminUser({
        username: form.username.trim(),
        display_name: form.display_name.trim(),
        password: form.password,
        role_id: form.role_id,
      })
      setForm(EMPTY_FORM)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось создать')
    }
  }

  async function removeUser(u: AdminUserOut) {
    if (!window.confirm(`Удалить пользователя ${u.username}?`)) return
    try {
      await apiClient.deleteAdminUser(u.id)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить')
    }
  }

  async function makeInvite() {
    const roleId = parseInt(inviteRole, 10)
    if (!Number.isFinite(roleId)) return
    try {
      const inv = await apiClient.createInvite(roleId)
      if (inv.token) {
        setInviteLink(`${location.origin}/?token=${inv.token}`)
      }
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось создать инвайт')
    }
  }

  return (
    <section className="details-section">
      <h3>Пользователи</h3>
      {users === null ? (
        <div className="muted">Загрузка…</div>
      ) : (
        users.map((u) => (
          <div key={u.id}>
            <div className="memory-item">
              <span className="memory-item-text">
                {u.display_name} <span className="muted">@{u.username}</span>
                <span className="muted">
                  {' '}
                  · {u.role}
                  {u.is_active ? '' : ' · отключён'}
                </span>
              </span>
              <button
                className="btn"
                onClick={() =>
                  editId === u.id ? setEditId(null) : startEdit(u)
                }
              >
                {editId === u.id ? 'Скрыть' : 'Изменить'}
              </button>
              <button
                className="icon-btn"
                title="Удалить пользователя"
                aria-label="Удалить пользователя"
                onClick={() => void removeUser(u)}
              >
                <IconTrash size={16} />
              </button>
            </div>
            {editId === u.id && draft !== null && (
              <div className="settings-form admin-edit">
                <input
                  value={draft.display_name}
                  onChange={(e) =>
                    setDraft({ ...draft, display_name: e.target.value })
                  }
                  placeholder="Имя"
                />
                <select
                  value={draft.role_id}
                  onChange={(e) =>
                    setDraft({ ...draft, role_id: Number(e.target.value) })
                  }
                >
                  {roles.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
                </select>
                <input
                  type="password"
                  value={draft.password}
                  onChange={(e) =>
                    setDraft({ ...draft, password: e.target.value })
                  }
                  placeholder="новый пароль (пусто — не менять)"
                  autoComplete="new-password"
                />
                <textarea
                  value={draft.prompt_fragment}
                  onChange={(e) =>
                    setDraft({ ...draft, prompt_fragment: e.target.value })
                  }
                  placeholder="фрагмент промпта пользователя"
                  rows={2}
                />
                <label className="settings-toggle">
                  <input
                    type="checkbox"
                    checked={draft.is_active}
                    onChange={(e) =>
                      setDraft({ ...draft, is_active: e.target.checked })
                    }
                  />
                  Активен
                </label>
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

      <h3>Новый пользователь</h3>
      <div className="settings-form">
        <input
          value={form.username}
          onChange={(e) => setForm({ ...form, username: e.target.value })}
          placeholder="логин"
          autoComplete="off"
        />
        <input
          value={form.display_name}
          onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          placeholder="имя"
        />
        <input
          type="password"
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
          placeholder="пароль"
          autoComplete="new-password"
        />
        <select
          value={form.role_id}
          onChange={(e) =>
            setForm({ ...form, role_id: Number(e.target.value) })
          }
        >
          <option value={0}>роль…</option>
          {roles.map((r) => (
            <option key={r.id} value={r.id}>
              {r.name}
            </option>
          ))}
        </select>
        <button
          className="btn"
          onClick={() => void createUser()}
          disabled={!form.username.trim() || !form.role_id || !form.password}
        >
          Создать
        </button>
      </div>

      {canInvite && (
        <>
          <h3>Приглашения</h3>
          <div className="settings-form">
            <select
              value={inviteRole}
              onChange={(e) => setInviteRole(e.target.value)}
            >
              <option value="">роль…</option>
              {roles.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
            <button
              className="btn"
              onClick={() => void makeInvite()}
              disabled={!inviteRole}
            >
              Создать ссылку
            </button>
          </div>
          {inviteLink && (
            <div className="link-block">
              <div className="link-code">{inviteLink}</div>
              <div className="muted">Одноразовая ссылка приглашения</div>
            </div>
          )}
          {invites.length > 0 && (
            <div className="muted">
              Активных приглашений:{' '}
              {invites.filter((i) => i.used_by === null).length} из{' '}
              {invites.length}
            </div>
          )}
        </>
      )}
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
