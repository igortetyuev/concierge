import { useEffect, useState } from 'react'
import { useStore } from '@nanostores/react'

import { apiClient, fetchAttachmentObjectUrl } from '../lib/api'
import type { MessagePart } from '../lib/api'
import { fmtBytes } from '../lib/format'
import { $attachments, setAttachment } from '../store/attachments'
import { $composerPrefill } from '../store/ui'
import ImageLightbox from './ImageLightbox'
import Markdown from './Markdown'
import { IconCheck, IconClose, IconCopy, IconFile, IconSpeaker, IconTool } from './icons'

export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) throw new Error('no clipboard api')
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      /* nothing */
    }
    ta.remove()
    return ok
  }
}

export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  async function onCopy() {
    if (!(await copyText(text))) return
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <button
      className="copy-btn"
      onClick={() => void onCopy()}
      aria-label="Копировать"
      title="Копировать"
    >
      {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
    </button>
  )
}

export function ToolChip({
  name,
  status,
  progress,
}: {
  name: string
  status?: string
  progress?: string
}) {
  const running = status === 'running' || !status
  return (
    <span className={`tool-chip status-${status ?? 'running'}`}>
      <IconTool size={13} />
      <span className="tool-name">{name}</span>
      {running && progress ? (
        <span className="tool-progress" title={progress}>
          {progress}
        </span>
      ) : null}
      {status === 'done' && <IconCheck size={13} />}
      {status === 'error' && <IconClose size={13} />}
      {running && <span className="spinner" />}
    </span>
  )
}

const TOOL_DETAILS_MAX = 4000

function fmtToolValue(v: unknown): string | null {
  if (v == null) return null
  const s = typeof v === 'string' ? v : JSON.stringify(v, null, 2)
  return s.length > TOOL_DETAILS_MAX ? `${s.slice(0, TOOL_DETAILS_MAX)}…` : s
}

/** Persisted tool call + its return: chip with expandable args/result. */
function ToolCallBlock({
  part,
  ret,
}: {
  part: MessagePart
  ret?: MessagePart
}) {
  const failed =
    ret != null &&
    (ret.retry === true ||
      ret.outcome === 'failed' ||
      ret.outcome === 'denied')
  const status = failed ? 'error' : 'done'
  const args = fmtToolValue(part.args)
  const result = fmtToolValue(ret?.content)
  const chip = (
    <ToolChip name={String(part.name ?? 'tool')} status={status} />
  )
  if (args == null && result == null) return chip
  return (
    <details className="tool-details">
      <summary>{chip}</summary>
      {args != null && <pre className="tool-io">{args}</pre>}
      {result != null && <pre className="tool-io">{result}</pre>}
    </details>
  )
}

interface AttachmentPartProps {
  part: MessagePart
  convId?: number
  origin?: string
  hasText: boolean
  autoplay: boolean
  onOpenImage?: (url: string) => void
}

function AttachmentPart({ part, convId, origin, hasText, autoplay, onOpenImage }: AttachmentPartProps) {
  const id = Number(part.attachment_id)
  // The WS attachment.updated event carries fresher derived data than the
  // expanded part (which was serialized when the message was fetched).
  const live = useStore($attachments)[id]
  const status = String(live?.status ?? part.status ?? 'pending')
  const name = String(live?.name ?? part.name ?? 'файл')
  const transcript = live?.transcript ?? part.transcript
  const caption = live?.caption ?? part.caption
  const derivedText = live?.text ?? part.text
  const error = live?.error ?? part.error

  const media = part.type === 'image' || part.type === 'audio' || part.type === 'video'
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!media || !id) return
    let objectUrl: string | null = null
    let alive = true
    void fetchAttachmentObjectUrl(id)
      .then((u) => {
        objectUrl = u
        if (alive) setUrl(u)
      })
      .catch(() => {
        if (alive) setFailed(true)
      })
    return () => {
      alive = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [id, media])

  async function download() {
    try {
      const u = await fetchAttachmentObjectUrl(id)
      const a = document.createElement('a')
      a.href = u
      a.download = name
      a.click()
      setTimeout(() => URL.revokeObjectURL(u), 10_000)
    } catch {
      /* keep silent — the chip just doesn't download */
    }
  }

  async function retryStt() {
    setBusy(true)
    try {
      setAttachment(await apiClient.retranscribe(id))
    } catch {
      /* the button just stays available */
    } finally {
      setBusy(false)
    }
  }

  const isVoice = part.type === 'audio' && origin === 'voice_quick'
  const canRetryStt = isVoice && (status === 'error' || status === 'skipped')
  const canSendTranscript =
    isVoice && status === 'done' && !!transcript && !hasText && convId != null

  const note =
    status === 'processing' || status === 'pending'
      ? 'обрабатывается…'
      : status === 'error'
        ? `не обработано${error ? `: ${String(error)}` : ''}`
        : null

  return (
    <span className="attachment">
      {part.type === 'image' &&
        (url ? (
          <img
            className="msg-image"
            src={url}
            alt={name}
            onClick={() => onOpenImage?.(url)}
          />
        ) : (
          <span className="part-attachment muted">{failed ? name : 'загрузка…'}</span>
        ))}
      {part.type === 'audio' &&
        (url ? (
          // eslint-disable-next-line jsx-a11y/media-has-caption -- transcript renders below
          <audio className="msg-audio" controls src={url} autoPlay={autoplay} />
        ) : (
          <span className="part-attachment muted">{failed ? name : 'загрузка…'}</span>
        ))}
      {part.type === 'video' &&
        (url ? (
          // eslint-disable-next-line jsx-a11y/media-has-caption -- transcript renders below
          <video className="msg-video" controls src={url} />
        ) : (
          <span className="part-attachment muted">{failed ? name : 'загрузка…'}</span>
        ))}
      {!media &&
        (part.type === 'document' ? (
          <button className="doc-card" onClick={() => void download()}>
            <IconFile size={20} />
            <span className="doc-card-info">
              <span className="doc-card-name">{name}</span>
              <span className="doc-card-meta muted">
                <span className="doc-card-ext">
                  {name.includes('.')
                    ? name.split('.').pop()!.toUpperCase()
                    : 'FILE'}
                </span>
                {typeof part.size === 'number' ? ` · ${fmtBytes(part.size)}` : ''}
              </span>
            </span>
          </button>
        ) : (
          <button className="part-attachment file" onClick={() => void download()}>
            <IconFile size={14} /> {name}
          </button>
        ))}
      {caption ? <span className="part-caption">{String(caption)}</span> : null}
      {transcript ? <span className="part-transcript">{String(transcript)}</span> : null}
      {derivedText && !transcript && part.generated !== true ? (
        <span className="part-transcript">
          {String(derivedText).length > 400 ? `${String(derivedText).slice(0, 400)}…` : String(derivedText)}
        </span>
      ) : null}
      {note && part.generated !== true && (
        <span className="part-note muted">{note}</span>
      )}
      {canRetryStt && (
        <button className="vq-act" onClick={() => void retryStt()} disabled={busy}>
          {busy ? 'Распознаю…' : 'Повторить распознавание'}
        </button>
      )}
      {canSendTranscript && (
        <button
          className="vq-act"
          onClick={() =>
            convId != null &&
            $composerPrefill.set({ convId, text: String(transcript) })
          }
        >
          Исправить и отправить
        </button>
      )}
    </span>
  )
}

/** Transcribed user text (voice message): plain text + a speaker button that
 * expands into an audio player on demand. */
function VoiceTextPart({
  part,
  attachmentId,
  mentions,
}: {
  part: MessagePart
  attachmentId: number
  mentions?: string[]
}) {
  const [open, setOpen] = useState(false)
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!open || url != null || failed) return
    let objectUrl: string | null = null
    let alive = true
    void fetchAttachmentObjectUrl(attachmentId)
      .then((u) => {
        objectUrl = u
        if (alive) setUrl(u)
      })
      .catch(() => {
        if (alive) setFailed(true)
      })
    return () => {
      alive = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [open, url, failed, attachmentId])

  return (
    <div className="voice-msg">
      <div className="voice-msg-row">
        <Markdown text={String(part.text ?? '')} mentions={mentions} />
        <button
          className={`voice-btn${open ? ' open' : ''}`}
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? 'Скрыть запись' : 'Прослушать запись'}
          title="Прослушать запись"
        >
          <IconSpeaker size={15} />
        </button>
      </div>
      {open &&
        (url ? (
          // eslint-disable-next-line jsx-a11y/media-has-caption -- transcript is the text itself
          <audio className="msg-audio" controls autoPlay src={url} />
        ) : (
          <span className="part-attachment muted">
            {failed ? 'аудио недоступно' : 'загрузка…'}
          </span>
        ))}
    </div>
  )
}

interface MessagePartsProps {
  parts: MessagePart[]
  convId?: number
  origin?: string
  /** Autoplay audio marked `tts` (a fresh voice-quick reply). */
  autoplayTts?: boolean
  /** Display names highlighted as @mentions inside text parts. */
  mentions?: string[]
}

export default function MessageParts({
  parts,
  convId,
  origin,
  autoplayTts = false,
  mentions,
}: MessagePartsProps) {
  const hasText = parts.some((p) => p.type === 'text')
  const imageParts = parts.filter((p) => p.type === 'image' && p.attachment_id != null)
  const toolReturns = new Map(
    parts
      .filter((p) => p.type === 'tool_return')
      .map((p) => [String(p.tool_call_id ?? ''), p]),
  )
  const [viewer, setViewer] = useState<{ idx: number; url: string } | null>(null)
  // Legacy voice messages store the recording as a separate audio part next
  // to the transcribed text; fold it into the speaker button instead of
  // rendering an inline player with a duplicated transcript.
  const needsFold = parts.some(
    (p) =>
      p.type === 'text' &&
      p.transcribed === true &&
      typeof p.voice_attachment_id !== 'number',
  )
  const foldedAudio = needsFold
    ? parts.find((p) => p.type === 'audio' && p.attachment_id != null)
    : undefined
  const foldedAudioId =
    foldedAudio != null ? Number(foldedAudio.attachment_id) : null
  // Consecutive thinking/tool parts collapse into one shared "рассуждение"
  // details block; tool_return parts ride along inside their ToolCallBlock.
  const workTypes = new Set(['thinking', 'tool', 'tool_call', 'tool_return'])
  const blocks: ({ work: true; items: MessagePart[] } | { work: false; p: MessagePart; i: number })[] = []
  parts.forEach((p, i) => {
    if (workTypes.has(p.type)) {
      const last = blocks[blocks.length - 1]
      if (last?.work) last.items.push(p)
      else blocks.push({ work: true, items: [p] })
    } else {
      blocks.push({ work: false, p, i })
    }
  })
  const renderWorkPart = (p: MessagePart, j: number) => {
    if (p.type === 'thinking') {
      return <Markdown key={j} text={String(p.content ?? '')} />
    }
    if (p.type === 'tool') {
      return (
        <ToolChip
          key={p.tool_call_id ?? j}
          name={String(p.name ?? 'tool')}
          status={p.status}
        />
      )
    }
    if (p.type === 'tool_call') {
      return (
        <ToolCallBlock
          key={String(p.tool_call_id ?? j)}
          part={p}
          ret={toolReturns.get(String(p.tool_call_id ?? ''))}
        />
      )
    }
    return null // tool_return — rendered inside the matching ToolCallBlock
  }
  return (
    <>
      {blocks.map((b, bi) => {
        if (b.work) {
          const hasThinking = b.items.some((p) => p.type === 'thinking')
          return (
            <details key={`w${bi}`} className="part-thinking">
              <summary className="muted">
                {hasThinking ? 'рассуждение' : 'инструменты'}
              </summary>
              {b.items.map(renderWorkPart)}
            </details>
          )
        }
        const { p, i } = b
        if (p.type === 'text') {
          const voiceId =
            typeof p.voice_attachment_id === 'number'
              ? p.voice_attachment_id
              : p.transcribed === true
                ? foldedAudioId
                : null
          if (voiceId != null) {
            return (
              <VoiceTextPart
                key={i}
                part={p}
                attachmentId={voiceId}
                mentions={mentions}
              />
            )
          }
          return (
            <Markdown key={i} text={String(p.text ?? '')} mentions={mentions} />
          )
        }
        if (p.type === 'mention') {
          return null
        }
        if (p.type === 'compaction') {
          const count = Number(p.count ?? 0)
          return (
            <details key={i} className="part-compaction">
              <summary className="muted">
                контекст сжат{count > 0 ? ` — ${count} сообщений в конспект` : ''}
              </summary>
              <Markdown text={String(p.text ?? '')} />
            </details>
          )
        }
        if (p.attachment_id != null) {
          if (Number(p.attachment_id) === foldedAudioId) return null
          const imgIdx = p.type === 'image' ? imageParts.indexOf(p) : -1
          return (
            <AttachmentPart
              key={String(p.attachment_id)}
              part={p}
              convId={convId}
              origin={origin}
              hasText={hasText}
              autoplay={autoplayTts && p.tts === true}
              onOpenImage={
                imgIdx >= 0 ? (url) => setViewer({ idx: imgIdx, url }) : undefined
              }
            />
          )
        }
        return (
          <span key={i} className="part-attachment">
            [{p.type}]
          </span>
        )
      })}
      {viewer != null && (
        <ImageLightbox
          images={imageParts.map((p) => ({
            id: Number(p.attachment_id),
            name: String(p.name ?? 'image'),
          }))}
          initialIndex={viewer.idx}
          initialUrl={viewer.url}
          onClose={() => setViewer(null)}
        />
      )}
    </>
  )
}
