import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'

import { apiClient } from '../lib/api'
import type { AttachmentOut, MessagePart } from '../lib/api'
import { sendTyping } from '../lib/ws'
import { sendParts, sendMessage } from '../store/messages'
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

function pickMime(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  const candidates = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm', '']
  for (const m of candidates) {
    if (!m || MediaRecorder.isTypeSupported(m)) return m
  }
  return ''
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

  function onChange(value: string) {
    setText(value)
    autoresize()
    const now = Date.now()
    if (value && now - lastTyping.current > TYPING_INTERVAL) {
      lastTyping.current = now
      sendTyping(convId)
    }
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
      const rec = new MediaRecorder(stream, { mimeType: pickMime() || undefined })
      chunksRef.current = []
      rec.ondataavailable = (e) => {
        if (e.data.size) chunksRef.current.push(e.data)
      }
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop())
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' })
        const ext = rec.mimeType?.includes('mp4') ? 'm4a' : 'webm'
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
    const preview =
      value.trim() || `[вложение] ${ready.map((s) => s.name).join(', ')}`
    for (const s of staged) if (s.previewUrl) URL.revokeObjectURL(s.previewUrl)
    setStaged([])
    if (parts.length === 1 && parts[0].type === 'text') {
      await sendMessage(convId, String(parts[0].text))
    } else {
      await sendParts(convId, parts, preview)
    }
    areaRef.current?.focus()
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void submit()
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
