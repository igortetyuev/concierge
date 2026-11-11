import { useEffect, useState } from 'react'
import { useStore } from '@nanostores/react'

import { fetchAttachmentObjectUrl } from '../lib/api'
import type { MessagePart } from '../lib/api'
import { $attachments } from '../store/attachments'
import Markdown from './Markdown'
import { IconCheck, IconClose, IconFile, IconTool } from './icons'

export function ToolChip({ name, status }: { name: string; status?: string }) {
  return (
    <span className={`tool-chip status-${status ?? 'running'}`}>
      <IconTool size={13} />
      <span className="tool-name">{name}</span>
      {status === 'done' && <IconCheck size={13} />}
      {status === 'error' && <IconClose size={13} />}
      {(status === 'running' || !status) && <span className="spinner" />}
    </span>
  )
}

function AttachmentPart({ part }: { part: MessagePart }) {
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
          <img className="msg-image" src={url} alt={name} />
        ) : (
          <span className="part-attachment muted">{failed ? name : 'загрузка…'}</span>
        ))}
      {part.type === 'audio' &&
        (url ? (
          // eslint-disable-next-line jsx-a11y/media-has-caption -- transcript renders below
          <audio className="msg-audio" controls src={url} />
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
      {!media && (
        <button className="part-attachment file" onClick={() => void download()}>
          <IconFile size={14} /> {name}
        </button>
      )}
      {caption ? <span className="part-caption">{String(caption)}</span> : null}
      {transcript ? <span className="part-transcript">{String(transcript)}</span> : null}
      {derivedText && !transcript ? (
        <span className="part-transcript">
          {String(derivedText).length > 400 ? `${String(derivedText).slice(0, 400)}…` : String(derivedText)}
        </span>
      ) : null}
      {note && <span className="part-note muted">{note}</span>}
    </span>
  )
}

export default function MessageParts({ parts }: { parts: MessagePart[] }) {
  return (
    <>
      {parts.map((p, i) => {
        if (p.type === 'text') return <Markdown key={i} text={String(p.text ?? '')} />
        if (p.type === 'tool') {
          return <ToolChip key={p.tool_call_id ?? i} name={String(p.name ?? 'tool')} status={p.status} />
        }
        if (p.attachment_id != null) {
          return <AttachmentPart key={String(p.attachment_id)} part={p} />
        }
        return (
          <span key={i} className="part-attachment">
            [{p.type}]
          </span>
        )
      })}
    </>
  )
}
