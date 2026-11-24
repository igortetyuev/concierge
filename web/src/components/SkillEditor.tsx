import { useMemo, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'

import { apiClient, ApiError } from '../lib/api'
import type { RoleOut, SkillOut, SkillScope } from '../lib/api'
import Markdown from './Markdown'
import { IconTrash } from './icons'

const NEW_SKILL_TEMPLATE = `---
name: my-skill
description: Короткое описание того, что делает скилл
---

Инструкции скилла в Markdown.
`

// Same shape as server skills/format.py _FM_RE.
const FM_RE = /^---[ \t]*\r?\n[\s\S]*?\r?\n?---[ \t]*\r?\n?/

function bodyOf(content: string): string {
  return content.replace(FM_RE, '')
}

interface Props {
  scope: SkillScope
  /** null — create mode. */
  skill: SkillOut | null
  /** Shared skills only: roles for the access checkboxes. */
  roles?: RoleOut[]
  onSaved: (skill: SkillOut) => void
  onClose: () => void
}

export default function SkillEditor({ scope, skill, roles = [], onSaved, onClose }: Props) {
  const isAdmin = scope === 'admin'
  const [content, setContent] = useState(skill?.content ?? NEW_SKILL_TEMPLATE)
  const [tab, setTab] = useState<'edit' | 'preview'>('edit')
  const [enabled, setEnabled] = useState(skill?.enabled ?? true)
  const [roleIds, setRoleIds] = useState<Set<number | string>>(
    new Set(skill?.role_ids ?? []),
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [filePath, setFilePath] = useState('')
  const [fileContent, setFileContent] = useState('')
  const importRef = useRef<HTMLInputElement>(null)

  const preview = useMemo(() => bodyOf(content), [content])

  async function save() {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const saved =
        skill == null
          ? await apiClient.createScopedSkill(
              scope,
              isAdmin
                ? { content, role_ids: [...roleIds], enabled }
                : { content },
            )
          : await apiClient.patchScopedSkill(
              scope,
              skill.id,
              isAdmin
                ? { content, role_ids: [...roleIds], enabled }
                : { content },
            )
      onSaved(saved)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Не удалось сохранить')
    } finally {
      setBusy(false)
    }
  }

  async function importFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setBusy(true)
    setError('')
    setNote('')
    try {
      const res = await apiClient.importScopedSkill(scope, file, file.name)
      if (res.skipped.length) {
        setNote(`Пропущено файлов: ${res.skipped.length} (${res.skipped.join(', ')})`)
      }
      onSaved(res.skill)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Не удалось импортировать')
    } finally {
      setBusy(false)
    }
  }

  async function exportSkill() {
    if (skill == null) return
    try {
      const { blob, filename } = await apiClient.exportScopedSkill(scope, skill.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      setTimeout(() => URL.revokeObjectURL(url), 10_000)
    } catch {
      setError('Не удалось экспортировать')
    }
  }

  async function saveFile() {
    if (skill == null || !filePath.trim()) return
    setBusy(true)
    setError('')
    try {
      await apiClient.putScopedSkillFile(scope, skill.id, filePath.trim(), fileContent)
      const fresh = await apiClient.getScopedSkill(scope, skill.id)
      setFilePath('')
      setFileContent('')
      onSaved(fresh)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Не удалось сохранить файл')
    } finally {
      setBusy(false)
    }
  }

  async function deleteFile(path: string) {
    if (skill == null) return
    setError('')
    try {
      await apiClient.deleteScopedSkillFile(scope, skill.id, path)
      onSaved(await apiClient.getScopedSkill(scope, skill.id))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Не удалось удалить файл')
    }
  }

  function toggleRole(id: number | string, on: boolean) {
    setRoleIds((cur) => {
      const next = new Set(cur)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
  }

  return (
    <div className="settings-form skill-editor">
      <div className="settings-row">
        <div className="skill-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={tab === 'edit'}
            className={tab === 'edit' ? 'active' : ''}
            onClick={() => setTab('edit')}
          >
            SKILL.md
          </button>
          <button
            role="tab"
            aria-selected={tab === 'preview'}
            className={tab === 'preview' ? 'active' : ''}
            onClick={() => setTab('preview')}
          >
            Просмотр
          </button>
        </div>
        <button className="btn" onClick={onClose}>
          Закрыть
        </button>
      </div>

      {tab === 'edit' ? (
        <textarea
          className="skill-md"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          spellCheck={false}
          rows={14}
        />
      ) : (
        <div className="skill-preview">
          <Markdown text={preview} />
        </div>
      )}

      {isAdmin && (
        <>
          <label className="settings-toggle">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            Скилл включён
          </label>
          {roles.length > 0 && (
            <div className="admin-perms">
              {roles.map((r) => (
                <label key={r.id} className="settings-toggle">
                  <input
                    type="checkbox"
                    checked={roleIds.has(r.id)}
                    onChange={(e) => toggleRole(r.id, e.target.checked)}
                  />
                  {r.name}
                </label>
              ))}
            </div>
          )}
          <div className="muted">Без отмеченных ролей скилл доступен всем.</div>
        </>
      )}

      {skill != null && (
        <div className="skill-files">
          <div className="muted">Файлы ({skill.files.length}/30)</div>
          {skill.files.map((p) => (
            <div key={p} className="memory-item">
              <span className="memory-item-text">{p}</span>
              <button
                className="btn"
                onClick={() => {
                  setFilePath(p)
                  setFileContent('')
                }}
                title="Перезаписать файл новым содержимым"
              >
                Изменить
              </button>
              <button
                className="icon-btn"
                title="Удалить файл"
                aria-label="Удалить файл"
                onClick={() => void deleteFile(p)}
              >
                <IconTrash size={16} />
              </button>
            </div>
          ))}
          <input
            value={filePath}
            onChange={(e) => setFilePath(e.target.value)}
            placeholder="references/файл.md"
          />
          <textarea
            value={fileContent}
            onChange={(e) => setFileContent(e.target.value)}
            placeholder="содержимое файла"
            rows={4}
          />
          <div className="settings-row">
            <button
              className="btn"
              disabled={busy || !filePath.trim()}
              onClick={() => void saveFile()}
            >
              Сохранить файл
            </button>
          </div>
        </div>
      )}

      <div className="settings-row">
        <input
          ref={importRef}
          type="file"
          accept=".md,.zip"
          hidden
          onChange={(e) => void importFile(e)}
        />
        <button className="btn" disabled={busy} onClick={() => importRef.current?.click()}>
          Импорт .md/.zip
        </button>
        {skill != null && (
          <button className="btn" onClick={() => void exportSkill()}>
            Экспорт
          </button>
        )}
        <button className="btn primary" disabled={busy} onClick={() => void save()}>
          {skill == null ? 'Создать' : 'Сохранить'}
        </button>
      </div>
      {note && <div className="muted">{note}</div>}
      {error && <div className="auth-error">{error}</div>}
    </div>
  )
}
