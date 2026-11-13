import { useEffect, useState } from 'react'

import { apiClient, fetchAttachmentObjectUrl } from '../../lib/api'
import type { AdminUserOut, VoiceOut, VoiceSampleOut } from '../../lib/api'
import { IconSpeaker, IconTrash } from '../icons'

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString('ru-RU') : '—'
}

async function playAttachment(attachmentId: number) {
  const url = await fetchAttachmentObjectUrl(attachmentId)
  const audio = new Audio(url)
  audio.onended = () => URL.revokeObjectURL(url)
  void audio.play()
}

export default function AdminVoices() {
  const [voices, setVoices] = useState<VoiceOut[] | null>(null)
  const [users, setUsers] = useState<AdminUserOut[]>([])
  const [samples, setSamples] = useState<Record<number, VoiceSampleOut[]>>({})
  const [openId, setOpenId] = useState<number | null>(null)
  const [mergeFrom, setMergeFrom] = useState<number | null>(null)
  const [mergeInto, setMergeInto] = useState('')
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [vs, us] = await Promise.all([
        apiClient.listVoices(),
        apiClient.listAdminUsers(),
      ])
      setVoices(vs)
      setUsers(us)
    } catch {
      setError('Не удалось загрузить данные')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function toggleSamples(v: VoiceOut) {
    if (openId === v.id) {
      setOpenId(null)
      return
    }
    setOpenId(v.id)
    if (samples[v.id] === undefined) {
      try {
        const list = await apiClient.listVoiceSamples(v.id)
        setSamples((s) => ({ ...s, [v.id]: list }))
      } catch {
        setError('Не удалось загрузить сэмплы')
      }
    }
  }

  async function saveLabel(v: VoiceOut, label: string) {
    const next = label.trim()
    if (!next || next === v.label) return
    try {
      await apiClient.patchVoice(v.id, { label: next })
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось сохранить')
    }
  }

  async function assignUser(v: VoiceOut, raw: string) {
    try {
      await apiClient.patchVoice(v.id, {
        user_id: raw === '' ? null : Number(raw),
      })
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось сохранить')
    }
  }

  async function doMerge(from: VoiceOut) {
    const intoId = parseInt(mergeInto, 10)
    if (!Number.isFinite(intoId)) return
    try {
      await apiClient.mergeVoice(from.id, intoId)
      setMergeFrom(null)
      setMergeInto('')
      setSamples({})
      setOpenId(null)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось объединить')
    }
  }

  async function removeVoice(v: VoiceOut) {
    if (!window.confirm(`Удалить ${v.label}?`)) return
    try {
      await apiClient.deleteVoice(v.id)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить')
    }
  }

  async function removeSample(v: VoiceOut, s: VoiceSampleOut) {
    try {
      await apiClient.deleteVoiceSample(v.id, s.id)
      const list = await apiClient.listVoiceSamples(v.id)
      setSamples((cur) => ({ ...cur, [v.id]: list }))
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить сэмпл')
    }
  }

  return (
    <section className="details-section">
      <h3>Голоса</h3>
      {voices === null ? (
        <div className="muted">Загрузка…</div>
      ) : voices.length === 0 ? (
        <div className="muted">
          Голосов пока нет — появятся после первых голосовых сообщений.
        </div>
      ) : (
        voices.map((v) => (
          <div key={v.id}>
            <div className="memory-item">
              <span className="memory-item-text">
                <input
                  className="voice-label"
                  defaultValue={v.label}
                  key={`${v.id}:${v.label}`}
                  onBlur={(e) => void saveLabel(v, e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter')
                      void saveLabel(v, (e.target as HTMLInputElement).value)
                  }}
                />
                <select
                  value={v.user_id ?? ''}
                  onChange={(e) => void assignUser(v, e.target.value)}
                >
                  <option value="">— не назначен —</option>
                  {users.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.display_name}
                    </option>
                  ))}
                </select>
                <span className="muted">
                  {' '}
                  сэмплов: {v.sample_count} · {fmtDate(v.last_heard_at)}
                </span>
              </span>
              {v.sample_attachment_id != null && (
                <button
                  className="icon-btn"
                  title="Прослушать образец"
                  aria-label="Прослушать образец"
                  onClick={() => void playAttachment(v.sample_attachment_id!)}
                >
                  <IconSpeaker size={16} />
                </button>
              )}
              <button
                className="btn"
                onClick={() => void toggleSamples(v)}
              >
                {openId === v.id ? 'Скрыть' : 'Сэмплы'}
              </button>
              <button
                className="btn"
                onClick={() => {
                  setMergeFrom(mergeFrom === v.id ? null : v.id)
                  setMergeInto('')
                }}
              >
                Объединить
              </button>
              <button
                className="icon-btn"
                title="Удалить голос"
                aria-label="Удалить голос"
                onClick={() => void removeVoice(v)}
              >
                <IconTrash size={16} />
              </button>
            </div>
            {mergeFrom === v.id && (
              <div className="settings-form admin-edit">
                <select
                  value={mergeInto}
                  onChange={(e) => setMergeInto(e.target.value)}
                >
                  <option value="">в какой голос…</option>
                  {voices
                    .filter((o) => o.id !== v.id)
                    .map((o) => (
                      <option key={o.id} value={o.id}>
                        {o.label}
                        {o.user_name ? ` (${o.user_name})` : ''}
                      </option>
                    ))}
                </select>
                <div className="settings-row">
                  <button
                    className="btn primary"
                    disabled={!mergeInto}
                    onClick={() => void doMerge(v)}
                  >
                    Объединить
                  </button>
                </div>
              </div>
            )}
            {openId === v.id && (
              <div className="admin-edit">
                {(samples[v.id] ?? []).map((s) => (
                  <div key={s.id} className="memory-item">
                    <span className="memory-item-text">
                      {s.attachment_name}
                      <span className="muted">
                        {' '}
                        · {s.score.toFixed(2)} · {fmtDate(s.created_at)}
                      </span>
                    </span>
                    <button
                      className="icon-btn"
                      title="Прослушать"
                      aria-label="Прослушать"
                      onClick={() => void playAttachment(s.attachment_id)}
                    >
                      <IconSpeaker size={16} />
                    </button>
                    <button
                      className="icon-btn"
                      title="Удалить сэмпл"
                      aria-label="Удалить сэмпл"
                      onClick={() => void removeSample(v, s)}
                    >
                      <IconTrash size={16} />
                    </button>
                  </div>
                ))}
                {(samples[v.id] ?? []).length === 0 && (
                  <div className="muted">Сэмплов нет</div>
                )}
              </div>
            )}
          </div>
        ))
      )}
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
