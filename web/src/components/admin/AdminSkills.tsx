import { useEffect, useState } from 'react'

import { apiClient } from '../../lib/api'
import type { RoleOut, SkillOut } from '../../lib/api'
import SkillEditor from '../SkillEditor'
import { IconTrash } from '../icons'

export default function AdminSkills() {
  const [skills, setSkills] = useState<SkillOut[] | null>(null)
  const [roles, setRoles] = useState<RoleOut[]>([])
  const [editId, setEditId] = useState<number | 'new' | null>(null)
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [ss, rs] = await Promise.all([
        apiClient.listScopedSkills('admin'),
        apiClient.listRoles(),
      ])
      setSkills(ss)
      setRoles(rs)
    } catch {
      setError('Не удалось загрузить скиллы')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function remove(s: SkillOut) {
    if (!window.confirm(`Удалить скилл «${s.name}»?`)) return
    try {
      await apiClient.deleteScopedSkill('admin', s.id)
      if (editId === s.id) setEditId(null)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить')
    }
  }

  function onSaved() {
    setEditId(null)
    void reload()
  }

  return (
    <section className="details-section">
      <h3>Скиллы</h3>
      {skills === null ? (
        <div className="muted">Загрузка…</div>
      ) : (
        <>
          {skills.length === 0 && (
            <div className="muted">
              Общих скиллов нет. Создайте SKILL.md или импортируйте архив.
            </div>
          )}
          {skills.map((s) => (
            <div key={s.id}>
              <div className="memory-item">
                <span className="memory-item-text">
                  /{s.name}
                  <span className="muted">
                    {' '}
                    · {s.description}
                    {!s.enabled ? ' · выключен' : ''}
                    {s.files.length ? ` · файлов: ${s.files.length}` : ''}
                  </span>
                </span>
                <button
                  className="btn"
                  onClick={() => setEditId(editId === s.id ? null : s.id)}
                >
                  {editId === s.id ? 'Скрыть' : 'Изменить'}
                </button>
                <button
                  className="icon-btn"
                  title="Удалить скилл"
                  aria-label="Удалить скилл"
                  onClick={() => void remove(s)}
                >
                  <IconTrash size={16} />
                </button>
              </div>
              {editId === s.id && (
                <div className="admin-edit">
                  <SkillEditor
                    scope="admin"
                    skill={s}
                    roles={roles}
                    onSaved={onSaved}
                    onClose={() => setEditId(null)}
                  />
                </div>
              )}
            </div>
          ))}
          <div className="settings-row">
            <button
              className="btn primary"
              onClick={() => setEditId(editId === 'new' ? null : 'new')}
            >
              Новый скилл
            </button>
          </div>
          {editId === 'new' && (
            <div className="admin-edit">
              <SkillEditor
                scope="admin"
                skill={null}
                roles={roles}
                onSaved={onSaved}
                onClose={() => setEditId(null)}
              />
            </div>
          )}
        </>
      )}
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
