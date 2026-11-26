import { Fragment, useEffect, useState } from 'react'
import { useStore } from '@nanostores/react'

import { apiClient, fetchAttachmentObjectUrl } from '../lib/api'
import type { ChoiceAnswer, MessagePart } from '../lib/api'
import { fmtBytes } from '../lib/format'
import { $attachments, setAttachment } from '../store/attachments'
import {
  $openWorkRuns,
  applyMessageCreated,
  setRunOpen,
} from '../store/messages'
import { $composerPrefill } from '../store/ui'
import ChecklistPart from './ChecklistPart'
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

// One playback at a time across all speak buttons.
let speakAudio: HTMLAudioElement | null = null
let speakStop: (() => void) | null = null

export function SpeakButton({
  convId,
  messageId,
}: {
  convId: number
  messageId: number
}) {
  const [state, setState] = useState<'idle' | 'loading' | 'playing'>('idle')

  async function toggle() {
    if (state === 'loading') return
    if (state === 'playing') {
      speakStop?.()
      return
    }
    setState('loading')
    speakStop?.()
    try {
      const { attachment_id } = await apiClient.speakMessage(convId, messageId)
      const url = await fetchAttachmentObjectUrl(attachment_id)
      const audio = new Audio(url)
      const stop = () => {
        audio.pause()
        URL.revokeObjectURL(url)
        if (speakAudio === audio) {
          speakAudio = null
          speakStop = null
        }
        setState('idle')
      }
      speakAudio = audio
      speakStop = stop
      audio.onended = stop
      audio.onerror = stop
      setState('playing')
      void audio.play().catch(stop)
    } catch {
      setState('idle')
    }
  }

  return (
    <button
      className="copy-btn"
      onClick={() => void toggle()}
      disabled={state === 'loading'}
      aria-label={state === 'playing' ? 'Остановить' : 'Озвучить'}
      title={state === 'playing' ? 'Остановить' : 'Озвучить'}
    >
      {state === 'loading' ? (
        <span className="spinner" />
      ) : state === 'playing' ? (
        <IconClose size={14} />
      ) : (
        <IconSpeaker size={14} />
      )}
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

function speakerName(part: MessagePart): string | null {
  const sp = part.speaker
  return sp && typeof sp.name === 'string' && sp.name ? sp.name : null
}

function SpeakerChip({ name }: { name: string | null }) {
  if (name == null) return null
  return (
    <span className="speaker-chip" title="Распознанный голос">
      <IconSpeaker size={11} />
      {name}
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
      <SpeakerChip name={live?.speaker?.name ?? speakerName(part)} />
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
      <SpeakerChip name={speakerName(part)} />
    </div>
  )
}

interface ChoiceOption {
  label: string
  description?: string
}

interface ChoiceQuestion {
  question: string
  header?: string
  options?: ChoiceOption[]
  multi_select?: boolean
}

/** `choice` part (ask_user_question): options card or open-question text. */
function ChoicePart({
  part,
  convId,
  messageId,
}: {
  part: MessagePart
  convId?: number
  messageId?: number
}) {
  const questions = (part.questions as ChoiceQuestion[] | undefined) ?? []
  const storedAnswers = (part.answers as ChoiceAnswer[] | undefined) ?? []
  const [sel, setSel] = useState<Record<number, string[]>>({})
  const [other, setOther] = useState<Record<number, string>>({})
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const answered = part.answered === true || done
  const interactive =
    !answered && convId != null && messageId != null
  const withOptions = questions
    .map((q, i) => ({ q, i }))
    .filter(({ q }) => (q.options?.length ?? 0) > 0)
  const canSubmit =
    interactive &&
    withOptions.length > 0 &&
    withOptions.every(
      ({ i }) =>
        (sel[i]?.length ?? 0) > 0 || (other[i] ?? '').trim().length > 0,
    )

  function toggle(qi: number, label: string, multi: boolean) {
    setSel((cur) => {
      const list = cur[qi] ?? []
      const next = multi
        ? list.includes(label)
          ? list.filter((x) => x !== label)
          : [...list, label]
        : [label]
      return { ...cur, [qi]: next }
    })
  }

  async function submit() {
    if (!canSubmit || convId == null || messageId == null) return
    setBusy(true)
    setError('')
    try {
      const reply = await apiClient.answerChoice(
        convId,
        messageId,
        String(part.choice_id ?? ''),
        questions.map((_q, i) => ({
          selected: sel[i] ?? [],
          other: (other[i] ?? '').trim() || null,
        })),
      )
      setDone(true)
      applyMessageCreated(reply)
    } catch {
      setError('Не удалось отправить ответ')
    } finally {
      setBusy(false)
    }
  }

  function pickedText(i: number): string {
    // Until message.updated lands, show the just-submitted selection.
    const ans = storedAnswers[i] ?? {
      selected: sel[i] ?? [],
      other: (other[i] ?? '').trim() || null,
    }
    const otherText = (ans.other ?? '').trim()
    return otherText ? `Другое: ${otherText}` : (ans.selected ?? []).join('; ')
  }

  return (
    <div className={`choice-card${answered ? ' done' : ''}`}>
      {questions.map((q, i) => (
        <div key={i} className="choice-q">
          {q.header ? <div className="choice-q-head">{q.header}</div> : null}
          <div className="choice-q-text">{q.question}</div>
          {(q.options?.length ?? 0) === 0 ? (
            answered && pickedText(i) ? (
              <div className="choice-answer muted">{pickedText(i)}</div>
            ) : (
              <div className="choice-open-hint muted">Ответьте в чате</div>
            )
          ) : answered ? (
            <div className="choice-answer muted">{pickedText(i)}</div>
          ) : (
            <>
              <div className="choice-opts">
                {(q.options ?? []).map((opt) => {
                  const checked = (sel[i] ?? []).includes(opt.label)
                  return (
                    <label key={opt.label} className="choice-opt">
                      <input
                        type={q.multi_select ? 'checkbox' : 'radio'}
                        name={`${String(part.choice_id ?? 'ch')}-${i}`}
                        checked={checked}
                        onChange={() =>
                          toggle(i, opt.label, q.multi_select === true)
                        }
                      />
                      <span>
                        {opt.label}
                        {opt.description ? (
                          <span className="choice-opt-desc">
                            {' '}
                            — {opt.description}
                          </span>
                        ) : null}
                      </span>
                    </label>
                  )
                })}
              </div>
              <div className="choice-other">
                <input
                  value={other[i] ?? ''}
                  onChange={(e) =>
                    setOther((cur) => ({ ...cur, [i]: e.target.value }))
                  }
                  placeholder="Другое…"
                />
              </div>
            </>
          )}
        </div>
      ))}
      {interactive && withOptions.length > 0 && (
        <button
          className="btn primary"
          disabled={!canSubmit || busy}
          onClick={() => void submit()}
        >
          Отправить
        </button>
      )}
      {error && <div className="auth-error">{error}</div>}
    </div>
  )
}

interface MessagePartsProps {
  parts: MessagePart[]
  convId?: number
  messageId?: number
  /** Run that produced this message; when set, the first work block's
   * "рассуждение" details stays open if the user opened it during streaming. */
  runId?: number | null
  origin?: string
  /** Autoplay audio marked `tts` (a fresh voice-quick reply). */
  autoplayTts?: boolean
  /** Display names highlighted as @mentions inside text parts. */
  mentions?: string[]
}

export default function MessageParts({
  parts,
  convId,
  messageId,
  runId,
  origin,
  autoplayTts = false,
  mentions,
}: MessagePartsProps) {
  const openWorkRuns = useStore($openWorkRuns)
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
  const firstWorkIdx = blocks.findIndex((b) => b.work)
  return (
    <>
      {blocks.map((b, bi) => {
        if (b.work) {
          const hasThinking = b.items.some((p) => p.type === 'thinking')
          if (runId != null && bi === firstWorkIdx) {
            return (
              <details
                key={`w${bi}`}
                className="part-thinking"
                open={openWorkRuns.has(runId)}
                onToggle={(e) => setRunOpen(runId, e.currentTarget.open)}
              >
                <summary className="muted">
                  {hasThinking ? 'рассуждение' : 'инструменты'}
                </summary>
                {b.items.map(renderWorkPart)}
              </details>
            )
          }
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
            <span key={i}>
              <Markdown text={String(p.text ?? '')} mentions={mentions} />
              <SpeakerChip name={speakerName(p)} />
            </span>
          )
        }
        if (p.type === 'mention') {
          return null
        }
        if (p.type === 'skill_event') {
          return (
            <span key={i}>
              Скилл «{String(p.name ?? '')}»{' '}
              {p.action === 'end' ? 'завершён' : 'запущен'}
            </span>
          )
        }
        if (p.type === 'choice') {
          return (
            <ChoicePart
              key={i}
              part={p}
              convId={convId}
              messageId={messageId}
            />
          )
        }
        if (p.type === 'checklist') {
          // Submissions to this checklist don't appear in the feed: the
          // hidden exchange is embedded into the part's `followups` and
          // shown in the card's own "рассуждение" details.
          const followups =
            (p.followups as
              | {
                  n?: number
                  answer?: string
                  parts?: MessagePart[] | null
                }[]
              | undefined) ?? []
          return (
            <Fragment key={i}>
              <ChecklistPart
                part={p}
                convId={convId}
                messageId={messageId}
              />
              {followups.length > 0 && (
                <details className="part-thinking checklist-followups">
                  <summary className="muted">рассуждение</summary>
                  {followups.map((f, fi) => (
                    <div key={f.n ?? fi} className="checklist-followup">
                      <div className="checklist-followup-answer muted">
                        {String(f.answer ?? '')}
                      </div>
                      {f.parts == null ? (
                        <span className="checklist-followup-pending muted">
                          <span className="spinner" /> обрабатывается…
                        </span>
                      ) : (
                        // Compact parts (checklists.compact_followup_parts)
                        // render flat — the outer details already is the
                        // collapsible "рассуждение" container.
                        f.parts.map((fp, pi) => {
                          if (fp.type === 'thinking') {
                            return (
                              <Markdown
                                key={pi}
                                text={String(fp.content ?? '')}
                              />
                            )
                          }
                          if (fp.type === 'tool') {
                            return (
                              <ToolChip
                                key={pi}
                                name={String(fp.name ?? 'tool')}
                                status={fp.status as string | undefined}
                              />
                            )
                          }
                          if (fp.type === 'text') {
                            return (
                              <Markdown
                                key={pi}
                                text={String(fp.text ?? '')}
                              />
                            )
                          }
                          return null
                        })
                      )}
                    </div>
                  ))}
                </details>
              )}
            </Fragment>
          )
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
