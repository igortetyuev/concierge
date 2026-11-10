import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import type { MemoryItemOut } from '../lib/api'
import { $user } from '../store/auth'
import { IconBack, IconTrash } from './icons'

export default function MemoryView() {
  const me = useStore($user)
  const [items, setItems] = useState<MemoryItemOut[] | null>(null)
  const [text, setText] = useState('')
  const [scope, setScope] = useState<'user' | 'family'>('user')
  const [busy, setBusy] = useState(false)

  const canFamily =
    me?.permissions['*'] === true || me?.permissions['family_memory'] === true

  async function reload() {
    try {
      setItems(await apiClient.listMemory())
    } catch {
      setItems([])
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function add() {
    const value = text.trim()
    if (!value || busy) return
    setBusy(true)
    try {
      await apiClient.addMemory(value, scope)
      setText('')
      await reload()
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: number) {
    try {
      await apiClient.deleteMemory(id)
      setItems((prev) => prev?.filter((i) => i.id !== id) ?? prev)
    } catch {
      /* server enforces permissions */
    }
  }

  const personal = (items ?? []).filter((i) => i.scope === 'user')
  const family = (items ?? []).filter((i) => i.scope === 'family')

  return (
    <div className="memory">
      <header className="chat-head">
        <Link to="/" className="icon-btn chat-back" aria-label="Назад">
          <IconBack />
        </Link>
        <div className="chat-head-text">
          <div className="chat-title">Память</div>
          <div className="chat-sub muted">
            Заметки, которые помнит ассистент
          </div>
        </div>
      </header>

      <div className="details-scroll">
        <section className="details-section">
          <div className="memory-form">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Новая заметка — например, «по субботам уборка»"
              maxLength={4000}
              rows={3}
            />
            <div className="memory-form-row">
              {canFamily && (
                <select
                  value={scope}
                  onChange={(e) =>
                    setScope(e.target.value as 'user' | 'family')
                  }
                  aria-label="Область"
                >
                  <option value="user">Личная</option>
                  <option value="family">Семейная</option>
                </select>
              )}
              <button
                className="btn primary"
                onClick={() => void add()}
                disabled={busy || !text.trim()}
              >
                Запомнить
              </button>
            </div>
          </div>
        </section>

        <section className="details-section">
          <h3>Личная</h3>
          {items === null && <div className="muted">Загрузка…</div>}
          {items !== null && personal.length === 0 && (
            <div className="muted">Пока пусто</div>
          )}
          {personal.map((i) => (
            <div key={i.id} className="memory-item">
              <span className="memory-item-text">{i.text}</span>
              <button
                className="icon-btn"
                title="Забыть"
                aria-label="Забыть"
                onClick={() => void remove(i.id)}
              >
                <IconTrash size={16} />
              </button>
            </div>
          ))}
        </section>

        <section className="details-section">
          <h3>Семейная</h3>
          {items !== null && family.length === 0 && (
            <div className="muted">Пока пусто</div>
          )}
          {family.map((i) => (
            <div key={i.id} className="memory-item">
              <span className="memory-item-text">{i.text}</span>
              {canFamily && (
                <button
                  className="icon-btn"
                  title="Забыть"
                  aria-label="Забыть"
                  onClick={() => void remove(i.id)}
                >
                  <IconTrash size={16} />
                </button>
              )}
            </div>
          ))}
        </section>
      </div>
    </div>
  )
}
