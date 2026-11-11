import { useEffect, useState } from 'react'
import { useStore } from '@nanostores/react'

import { apiClient, fetchAttachmentObjectUrl } from '../lib/api'
import type { MessagePart } from '../lib/api'
import { fmtBytes } from '../lib/format'
import { $attachments, setAttachment } from '../store/attachments'
import { $composerPrefill } from '../store/ui'
import ImageLightbox from './ImageLightbox'
import Markdown from './Markdown'
import { IconCheck, IconClose, IconCopy, IconFile, IconTool } from './icons'

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
  const [viewer, setViewer] = useState<{ idx: number; url: string } | null>(null)
  return (
    <>
      {parts.map((p, i) => {
        if (p.type === 'text') {
          if (p.transcribed === true && convId != null) {
            return (
              <div key={i} className="transcribed">
                <Markdown text={String(p.text ?? '')} mentions={mentions} />
                <span className="transcribed-tag muted">
                  распознано
                  <button
                    className="vq-act"
                    onClick={() =>
                      $composerPrefill.set({
                        convId,
                        text: String(p.text ?? ''),
                      })
                    }
                  >
                    исправить
                  </button>
                </span>
              </div>
            )
          }
          return (
            <Markdown key={i} text={String(p.text ?? '')} mentions={mentions} />
          )
        }
        if (p.type === 'mention') {
          return null
        }
        if (p.type === 'tool') {
          return <ToolChip key={p.tool_call_id ?? i} name={String(p.name ?? 'tool')} status={p.status} />
        }
        if (p.attachment_id != null) {
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
