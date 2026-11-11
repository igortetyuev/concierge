import { useEffect, useState } from 'react'

import { apiClient } from '../../lib/api'
import type { AdminUserOut } from '../../lib/api'

export default function AdminPrompt() {
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('')
  const [users, setUsers] = useState<AdminUserOut[]>([])
  const [previewUser, setPreviewUser] = useState('')
  const [preview, setPreview] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [gs, us] = await Promise.all([
        apiClient.listGlobalSettings(),
        apiClient.listAdminUsers(),
      ])
      setName(String(gs['agent.name'] ?? ''))
      setPrompt(String(gs['agent.system_prompt'] ?? ''))
      setUsers(us)
    } catch {
      setError('Не удалось загрузить настройки')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function save() {
    setSaved(false)
    try {
      await apiClient.putGlobalSetting('agent.name', name.trim() || 'Консьерж')
      if (prompt.trim())
        await apiClient.putGlobalSetting('agent.system_prompt', prompt)
      setSaved(true)
    } catch {
      setError('Не удалось сохранить')
    }
  }

  async function loadPreview() {
    const uid = parseInt(previewUser, 10)
    if (!Number.isFinite(uid)) return
    setPreview('')
    try {
      const res = await apiClient.promptPreview(uid)
      setPreview(res.prompt)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось собрать промпт')
    }
  }

  return (
    <section className="details-section">
      <h3>Системный промпт</h3>
      <label className="field">
        <span>Имя ассистента</span>
        <input
          value={name}
          placeholder="Консьерж"
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="field">
        <span>Базовый промпт (placeholder: {'{name}'})</span>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={6}
        />
      </label>
      <div className="settings-row">
        <button className="btn primary" onClick={() => void save()}>
          Сохранить
        </button>
        {saved && <span className="muted">сохранено</span>}
      </div>
      <div className="muted">
        Фрагменты ролей и пользователей редактируются в разделах «Роли» и
        «Пользователи».
      </div>

      <h3>Предпросмотр</h3>
      <div className="settings-form">
        <select
          value={previewUser}
          onChange={(e) => setPreviewUser(e.target.value)}
        >
          <option value="">пользователь…</option>
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.display_name} ({u.role})
            </option>
          ))}
        </select>
        <button
          className="btn"
          onClick={() => void loadPreview()}
          disabled={!previewUser}
        >
          Собрать промпт
        </button>
      </div>
      {preview && <pre className="prompt-preview">{preview}</pre>}
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
