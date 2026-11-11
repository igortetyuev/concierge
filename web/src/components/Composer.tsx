import { useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import type { AttachmentOut, MessagePart } from '../lib/api'
import { audioExt, pickAudioMime } from '../lib/audio'
import { initials } from '../lib/format'
import { mentionRegex } from '../lib/mentions'
import { loadPromptHistory, pushPromptHistory } from '../lib/promptHistory'
import { sendTyping } from '../lib/ws'
import { $conversations } from '../store/conversations'
import { sendParts, sendMessage } from '../store/messages'
import { $composerPrefill } from '../store/ui'
import { IconCamera, IconClose, IconMic, IconPaperclip, IconSend, IconStop } from './icons'

const TYPING_INTERVAL = 3000
const RECORD_LIMIT_S = 120

interface Staged {
  key: string
  name: string
  previewUrl: string | null
  attachment: AttachmentOut | null
  error: string | null
}

interface MentionTarget {
  key: string
  name: string
  userId: number | null
}

/** `@` token under the caret: start index + typed query, or null. */
function detectMention(
  value: string,
  caret: number,
): { start: number; query: string } | null {
  const before = value.slice(0, caret)
  const at = before.lastIndexOf('@')
  if (at < 0) return null
  if (at > 0 && !/\s/.test(before[at - 1])) return null
  const query = before.slice(at + 1)
  if (query.includes('\n') || query.length > 30) return null
  return { start: at, query }
}

export default function Composer({ convId }: { convId: number }) {
  const [text, setText] = useState('')
  const [staged, setStaged] = useState<Staged[]>([])
  const [recording, setRecording] = useState(false)
  const [recSeconds, setRecSeconds] = useState(0)
  const areaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const cameraRef = useRef<HTMLInputElement>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const lastTyping = useRef(0)
  const historyRef = useRef<string[] | null>(null)
  const histIdxRef = useRef(-1)
  const draftRef = useRef('')
  const prefill = useStore($composerPrefill)
  const conv = useStore($conversations).find((c) => c.id === convId)
  const [mention, setMention] = useState<{
    start: number
    query: string
  } | null>(null)
  const [mentionIdx, setMentionIdx] = useState(0)

  const mentionTargets = useMemo<MentionTarget[]>(() => {
    if (conv?.kind !== 'group') return []
    return [
      {
        key: 'assistant',
        name: conv.assistant_name || 'Консьерж',
        userId: null,
      },
      ...conv.members.map((m) => ({
        key: `u${m.user_id}`,
        name: m.display_name,
        userId: m.user_id,
      })),
    ]
  }, [conv])

  const mentionQuery = mention?.query.toLowerCase() ?? ''
  const mentionList = mention
    ? mentionTargets.filter((t) =>
        t.name.toLowerCase().includes(mentionQuery),
      )
    : []
  const mentionOpen = mention != null && mentionList.length > 0
  const activeMention = Math.min(mentionIdx, mentionList.length - 1)

  // ArrowUp/ArrowDown recall index resets when the conversation changes.
  useEffect(() => {
    histIdxRef.current = -1
    draftRef.current = ''
    setMention(null)
  }, [convId])

  useEffect(() => {
    setMentionIdx(0)
  }, [mentionQuery])

  function autoresize() {
    const el = areaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  useEffect(() => {
    if (!recording) return
    const t = setInterval(() => {
      setRecSeconds((s) => {
        if (s + 1 >= RECORD_LIMIT_S) stopRecording()
        return s + 1
      })
    }, 1000)
    return () => clearInterval(t)
  }, [recording])

  useEffect(() => () => {
    for (const s of staged) if (s.previewUrl) URL.revokeObjectURL(s.previewUrl)
    recorderRef.current?.stop()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // «Исправить и отправить заново» drops the transcript into the composer.
  useEffect(() => {
    if (!prefill || prefill.convId !== convId) return
    $composerPrefill.set(null)
    setMention(null)
    setText(prefill.text)
    requestAnimationFrame(() => {
      autoresize()
      areaRef.current?.focus()
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill, convId])

  function onChange(value: string, caret?: number) {
    setText(value)
    autoresize()
    const el = areaRef.current
    setMention(
      detectMention(value, caret ?? el?.selectionStart ?? value.length),
    )
    const now = Date.now()
    if (value && now - lastTyping.current > TYPING_INTERVAL) {
      lastTyping.current = now
      sendTyping(convId)
    }
  }

  function completeMention(target: MentionTarget) {
    const el = areaRef.current
    if (!el || mention == null) return
    const caret = el.selectionStart ?? text.length
    const insert = `@${target.name} `
    const next = text.slice(0, mention.start) + insert + text.slice(caret)
    const pos = mention.start + insert.length
    onChange(next, pos)
    requestAnimationFrame(() => {
      el.focus()
      el.selectionStart = el.selectionEnd = pos
    })
  }

  function stage(file: File | Blob, name: string) {
    const key = crypto.randomUUID()
    const previewUrl = file.type.startsWith('image/') ? URL.createObjectURL(file) : null
    const entry: Staged = { key, name, previewUrl, attachment: null, error: null }
    setStaged((list) => [...list, entry])
    void apiClient
      .uploadAttachment(file, name, convId)
      .then((att) =>
        setStaged((list) => list.map((s) => (s.key === key ? { ...s, attachment: att } : s))),
      )
      .catch(() =>
        setStaged((list) =>
          list.map((s) => (s.key === key ? { ...s, error: 'не загружено' } : s)),
        ),
      )
  }

  function onFiles(e: ChangeEvent<HTMLInputElement>) {
    for (const f of Array.from(e.target.files ?? [])) stage(f, f.name)
    e.target.value = ''
  }

  function unstage(key: string) {
    setStaged((list) => {
      const gone = list.find((s) => s.key === key)
      if (gone?.previewUrl) URL.revokeObjectURL(gone.previewUrl)
      return list.filter((s) => s.key !== key)
    })
  }

  async function startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const rec = new MediaRecorder(stream, { mimeType: pickAudioMime() || undefined })
      chunksRef.current = []
      rec.ondataavailable = (e) => {
        if (e.data.size) chunksRef.current.push(e.data)
      }
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop())
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' })
        const ext = audioExt(rec.mimeType || '')
        if (blob.size) stage(blob, `voice.${ext}`)
      }
      recorderRef.current = rec
      setRecSeconds(0)
      setRecording(true)
      rec.start()
    } catch {
      /* mic denied or unavailable */
    }
  }

  function stopRecording() {
    recorderRef.current?.stop()
    recorderRef.current = null
    setRecording(false)
  }

  const uploading = staged.some((s) => s.attachment == null && !s.error)

  async function submit() {
    const value = text
    const ready = staged.filter((s) => s.attachment != null)
    setText('')
    setMention(null)
    requestAnimationFrame(autoresize)
    if (!value.trim() && !ready.length) return
    const parts: MessagePart[] = []
    if (value.trim()) parts.push({ type: 'text', text: value.trim() })
    for (const s of ready) {
      const att = s.attachment as AttachmentOut
      parts.push({
        type: att.kind === 'other' ? 'file' : att.kind,
        attachment_id: att.id,
        name: s.name,
        mime: att.mime,
      })
    }
    if (value.trim() && mentionTargets.length) {
      const re = mentionRegex(mentionTargets.map((t) => t.name))
      const seen = new Set<string>()
      if (re) {
        for (const m of value.matchAll(re)) {
          const key = m[0].slice(1).toLowerCase()
          if (seen.has(key)) continue
          seen.add(key)
          const t = mentionTargets.find(
            (x) => x.name.toLowerCase() === key,
          )
          if (!t) continue
          parts.push(
            t.userId == null
              ? { type: 'mention', assistant: true, name: t.name }
              : { type: 'mention', user_id: t.userId, name: t.name },
          )
        }
      }
    }
    const preview =
      value.trim() || `[вложение] ${ready.map((s) => s.name).join(', ')}`
    for (const s of staged) if (s.previewUrl) URL.revokeObjectURL(s.previewUrl)
    setStaged([])
    if (value.trim()) {
      pushPromptHistory(value)
      historyRef.current = null
    }
    histIdxRef.current = -1
    if (parts.length === 1 && parts[0].type === 'text') {
      await sendMessage(convId, String(parts[0].text))
    } else {
      await sendParts(convId, parts, preview)
    }
    areaRef.current?.focus()
  }

  function recall(value: string) {
    onChange(value)
    requestAnimationFrame(() => {
      const el = areaRef.current
      if (el) el.selectionStart = el.selectionEnd = value.length
    })
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (mentionOpen && !e.nativeEvent.isComposing) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setMentionIdx((i) => (i + 1) % mentionList.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setMentionIdx(
          (i) => (i - 1 + mentionList.length) % mentionList.length,
        )
        return
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault()
        const target = mentionList[activeMention]
        if (target) completeMention(target)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setMention(null)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void submit()
      return
    }
    if (
      e.nativeEvent.isComposing ||
      e.shiftKey ||
      e.altKey ||
      e.ctrlKey ||
      e.metaKey
    )
      return
    const el = e.currentTarget
    if (el.selectionStart !== el.selectionEnd) return
    historyRef.current ??= loadPromptHistory()
    const hist = historyRef.current
    if (e.key === 'ArrowUp' && !el.value.slice(0, el.selectionStart).includes('\n')) {
      if (!hist.length) return
      e.preventDefault()
      if (histIdxRef.current === -1) {
        draftRef.current = el.value
        histIdxRef.current = hist.length - 1
      } else {
        histIdxRef.current = Math.max(0, histIdxRef.current - 1)
      }
      recall(hist[histIdxRef.current])
    } else if (
      e.key === 'ArrowDown' &&
      histIdxRef.current !== -1 &&
      !el.value.slice(el.selectionEnd).includes('\n')
    ) {
      e.preventDefault()
      if (histIdxRef.current >= hist.length - 1) {
        histIdxRef.current = -1
        recall(draftRef.current)
      } else {
        histIdxRef.current += 1
        recall(hist[histIdxRef.current])
      }
    }
  }

  const canSend = (text.trim().length > 0 || staged.some((s) => s.attachment)) && !uploading

  return (
    <div className="composer-wrap">
      {staged.length > 0 && (
        <div className="attach-strip">
          {staged.map((s) => (
            <span key={s.key} className={`attach-chip${s.error ? ' error' : ''}`}>
              {s.previewUrl ? (
                <img className="attach-thumb" src={s.previewUrl} alt="" />
              ) : null}
              <span className="attach-name">{s.name}</span>
              {s.error ? (
                <span className="attach-err">{s.error}</span>
              ) : s.attachment == null ? (
                <span className="spinner" />
              ) : null}
              <button className="mini-btn" onClick={() => unstage(s.key)} aria-label="Убрать">
                <IconClose size={13} />
              </button>
            </span>
          ))}
        </div>
      )}
      {mentionOpen && (
        <div className="mention-pop" role="listbox">
          {mentionList.map((t, i) => (
            <button
              key={t.key}
              type="button"
              role="option"
              aria-selected={i === activeMention}
              className={`mention-item${i === activeMention ? ' active' : ''}`}
              onMouseDown={(e) => {
                e.preventDefault()
                completeMention(t)
              }}
              onMouseEnter={() => setMentionIdx(i)}
            >
              <span className="avatar small">{initials(t.name)}</span>
              <span className="mention-name">{t.name}</span>
              {t.userId == null && <span className="muted">ассистент</span>}
            </button>
          ))}
        </div>
      )}
      <div className="composer">
        <input ref={fileRef} type="file" multiple hidden onChange={onFiles} />
        <input
          ref={cameraRef}
          type="file"
          accept="image/*"
          capture="environment"
          hidden
          onChange={onFiles}
        />
        <button
          className="icon-btn"
          onClick={() => fileRef.current?.click()}
          aria-label="Прикрепить файл"
          title="Прикрепить файл"
        >
          <IconPaperclip />
        </button>
        <button
          className="icon-btn"
          onClick={() => cameraRef.current?.click()}
          aria-label="Камера"
          title="Камера"
        >
          <IconCamera />
        </button>
        {recording ? (
          <button
            className="icon-btn recording"
            onClick={stopRecording}
            aria-label="Остановить запись"
            title={`Запись ${recSeconds} с — стоп`}
          >
            <IconStop />
          </button>
        ) : (
          <button
            className="icon-btn"
            onClick={() => void startRecording()}
            aria-label="Голосовое сообщение"
            title="Голосовое сообщение"
          >
            <IconMic />
          </button>
        )}
        {recording ? (
          <span className="rec-timer">
            <span className="rec-dot" /> {Math.floor(recSeconds / 60)}:
            {String(recSeconds % 60).padStart(2, '0')}
          </span>
        ) : (
          <textarea
            ref={areaRef}
            rows={1}
            value={text}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            onSelect={(e) =>
              setMention(
                e.currentTarget.selectionStart === e.currentTarget.selectionEnd
                  ? detectMention(
                      e.currentTarget.value,
                      e.currentTarget.selectionStart,
                    )
                  : null,
              )
            }
            onBlur={() => setMention(null)}
            placeholder="Сообщение"
            aria-label="Сообщение"
          />
        )}
        <button
          className="icon-btn send"
          onClick={() => void submit()}
          disabled={!canSend || recording}
          aria-label="Отправить"
        >
          <IconSend />
        </button>
      </div>
    </div>
  )
}
