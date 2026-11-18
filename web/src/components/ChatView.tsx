import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { MouseEvent, PointerEvent, ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import { useStore } from '@nanostores/react'

import type { MessageOut } from '../lib/api'
import { dayLabel, fmtTime, initials, isSameDay } from '../lib/format'
import { $user } from '../store/auth'
import {
  $activeId,
  $conversations,
  $online,
  $readSeqs,
  $typing,
  convTitle,
  patchConversation,
  typingUsers,
  refreshConversation,
} from '../store/conversations'
import {
  $hasMore,
  $loadingMore,
  $messages,
  $pending,
  $streams,
  $ttsAutoplay,
  deleteMessage,
  discardPending,
  loadInitial,
  loadOlder,
  retryPending,
} from '../store/messages'
import type { PendingMessage, StreamState } from '../store/messages'
import { markLatestRead } from '../store/realtime'
import { $detailsOpen } from '../store/ui'
import Composer from './Composer'
import DetailsPanel from './DetailsPanel'
import ForwardDialog from './ForwardDialog'
import MessageParts, { CopyButton, ToolChip } from './MessageParts'
import Markdown from './Markdown'
import {
  IconBack,
  IconCheck,
  IconCheckAll,
  IconClose,
  IconDown,
  IconForward,
  IconInfo,
  IconRetry,
  IconTrash,
} from './icons'

const EMPTY_MSGS: MessageOut[] = []
const EMPTY_PENDING: PendingMessage[] = []
const EMPTY_STREAMS: Record<number, StreamState> = {}
const EMPTY_IDS: number[] = []
const EMPTY_READ: Record<number, number> = {}

const SWIPE_OPEN_W = 72
const SWIPE_COMMIT = 140

/**
 * Swipe-left-to-delete wrapper for touch pointers. A short swipe opens the
 * delete action; a long one commits it. Vertical scrolling is left to the
 * browser (`touch-action: pan-y`), mouse pointers are ignored — hover UI
 * covers them.
 */
function SwipeRow({
  enabled,
  onDelete,
  children,
}: {
  enabled: boolean
  onDelete: () => void
  children: ReactNode
}) {
  const [dx, setDx] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [open, setOpen] = useState(false)
  const gesture = useRef<{ x: number; y: number; horiz: boolean } | null>(null)
  const suppressClick = useRef(false)

  if (!enabled) return <>{children}</>

  function close(): void {
    setDx(0)
    setOpen(false)
  }

  function onPointerDown(e: PointerEvent<HTMLDivElement>): void {
    if (e.pointerType !== 'touch') return
    gesture.current = { x: e.clientX, y: e.clientY, horiz: false }
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>): void {
    const g = gesture.current
    if (!g) return
    const mx = e.clientX - g.x
    const my = e.clientY - g.y
    if (!g.horiz) {
      if (Math.abs(mx) < 10 && Math.abs(my) < 10) return
      if (Math.abs(my) >= Math.abs(mx)) {
        gesture.current = null // vertical — the scroller takes over
        return
      }
      g.horiz = true
      setDragging(true)
    }
    const base = open ? -SWIPE_OPEN_W : 0
    setDx(Math.min(0, Math.max(-SWIPE_COMMIT - 32, base + mx)))
  }

  function onPointerUp(): void {
    const g = gesture.current
    gesture.current = null
    if (!g?.horiz) return
    setDragging(false)
    suppressClick.current = true
    if (dx <= -SWIPE_COMMIT) {
      close()
      onDelete()
    } else if (dx <= -SWIPE_OPEN_W / 2) {
      setDx(-SWIPE_OPEN_W)
      setOpen(true)
    } else {
      close()
    }
  }

  function onPointerCancel(): void {
    const wasHoriz = gesture.current?.horiz
    gesture.current = null
    setDragging(false)
    if (wasHoriz) {
      suppressClick.current = true
      setDx(open ? -SWIPE_OPEN_W : 0)
    }
  }

  function onClickCapture(e: MouseEvent<HTMLDivElement>): void {
    const onDel = (e.target as HTMLElement).closest('.swipe-del') != null
    if (suppressClick.current) {
      suppressClick.current = false
      if (!onDel) {
        e.preventDefault()
        e.stopPropagation()
      }
      return
    }
    if (open && !onDel) {
      e.preventDefault()
      e.stopPropagation()
      close()
    }
  }

  return (
    <div
      className={`swipe${dragging ? ' dragging' : ''}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onClickCapture={onClickCapture}
    >
      <button
        className="swipe-del"
        style={{ opacity: dx < 0 ? 1 : 0 }}
        tabIndex={-1}
        aria-hidden={dx >= 0}
        aria-label="Удалить сообщение"
        onClick={(e) => {
          e.stopPropagation()
          close()
          onDelete()
        }}
      >
        <IconTrash />
      </button>
      <div className="swipe-inner" style={{ transform: `translateX(${dx}px)` }}>
        {children}
      </div>
    </div>
  )
}

export default function ChatView({ convId }: { convId: number }) {
  const navigate = useNavigate()
  const me = useStore($user)
  const conv = useStore($conversations).find((c) => c.id === convId)
  const msgs = useStore($messages)[convId] ?? EMPTY_MSGS
  const pending = useStore($pending)[convId] ?? EMPTY_PENDING
  const streams = useStore($streams)[convId] ?? EMPTY_STREAMS
  const hasMore = useStore($hasMore)[convId] ?? false
  const loadingMore = useStore($loadingMore)[convId] ?? false
  const online = useStore($online)[convId] ?? EMPTY_IDS
  const readSeqs = useStore($readSeqs)[convId] ?? EMPTY_READ
  const detailsOpen = useStore($detailsOpen)
  useStore($typing) // subscribe for typing expiry updates

  const scrollRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const pendingAdjust = useRef<number | null>(null)
  const [showJump, setShowJump] = useState(false)
  const [, setTick] = useState(0)
  const [selecting, setSelecting] = useState(false)
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [forwardOpen, setForwardOpen] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)

  useEffect(() => {
    $activeId.set(convId)
    setSelecting(false)
    setPicked(new Set())
    void refreshConversation(convId)
    void loadInitial(convId).then(() => markLatestRead(convId))
    return () => $activeId.set(null)
  }, [convId])

  // A session deleted while open is hidden from non-admins.
  useEffect(() => {
    if (conv?.status === 'deleted' && me?.permissions['*'] !== true) {
      $detailsOpen.set(false)
      navigate('/')
    }
  }, [conv?.status, me?.permissions, navigate])

  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1000)
    const onVis = () => {
      if (document.visibilityState === 'visible') markLatestRead(convId)
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      clearInterval(t)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [convId])

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (pendingAdjust.current != null) {
      el.scrollTop += el.scrollHeight - pendingAdjust.current
      pendingAdjust.current = null
      return
    }
    if (stick.current) el.scrollTop = el.scrollHeight
  }, [msgs, pending, streams])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    setShowJump(!stick.current)
    if (el.scrollTop < 60 && hasMore && !loadingMore) {
      pendingAdjust.current = el.scrollHeight
      void loadOlder(convId)
    }
  }

  function jumpToLatest() {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  function authorName(id: number | null): string {
    if (id == null) return conv?.assistant_name || 'Консьерж'
    return conv?.members.find((m) => m.user_id === id)?.display_name ?? 'Участник'
  }

  function readByAll(m: MessageOut): boolean {
    const others = (conv?.members ?? []).filter((x) => x.user_id !== me?.id)
    return others.length > 0 && others.every((x) => (readSeqs[x.user_id] ?? 0) >= m.seq)
  }

  async function onDeleteMessage(m: MessageOut): Promise<void> {
    if (!window.confirm('Удалить сообщение?')) return
    try {
      await deleteMessage(convId, m.id)
    } catch {
      window.alert('Не удалось удалить сообщение')
    }
  }

  function togglePick(id: number): void {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function exitSelect(): void {
    setSelecting(false)
    setPicked(new Set())
    setForwardOpen(false)
  }

  async function onDeletePicked(): Promise<void> {
    const ids = msgs.filter((m) => picked.has(m.id)).map((m) => m.id)
    if (!ids.length || actionBusy) return
    if (!window.confirm(`Удалить сообщений: ${ids.length}?`)) return
    setActionBusy(true)
    let failed = 0
    for (const id of ids) {
      try {
        await deleteMessage(convId, id)
      } catch {
        failed += 1
      }
    }
    setActionBusy(false)
    exitSelect()
    if (failed) window.alert(`Не удалось удалить: ${failed}`)
  }

  function onForwarded(targetConvId: number): void {
    exitSelect()
    navigate(`/c/${targetConvId}`)
  }

  const typing = typingUsers(convId).filter((id) => id !== me?.id)
  const onlineOthers = online.filter((id) => id !== me?.id)
  const streamList = Object.values(streams).sort((a, b) => a.runId - b.runId)
  const archived = conv?.status === 'archived'
  const deleted = conv?.status === 'deleted'
  const canManage = conv != null && (conv.owner_id === me?.id || me?.permissions['*'] === true)
  const mentionNames =
    conv?.kind === 'group'
      ? [conv.assistant_name, ...conv.members.map((m) => m.display_name)]
      : undefined

  // Only a just-arrived voice reply autoplays; history never does.
  // Only a just-arrived (WS) voice reply autoplays; history never does.
  const ttsAutoplayId = useStore($ttsAutoplay)[convId]

  return (
    <div className="chat">
      <header className="chat-head">
        <Link to="/" className="icon-btn chat-back" aria-label="Назад">
          <IconBack />
        </Link>
        <span className="avatar">{initials(convTitle(conv).slice(0, 2))}</span>
        <div className="chat-head-text">
          <div className="chat-title">{convTitle(conv)}</div>
          <div className="chat-sub muted">
            {typing.length > 0
              ? `${typing.map(authorName).join(', ')} печатает…`
              : conv?.kind === 'group'
                ? `${conv.members.length} уч. · онлайн: ${onlineOthers.length}`
                : online.length > 0
                  ? 'в сети'
                  : ''}
          </div>
        </div>
        {msgs.length > 0 && (
          <button
            className="icon-btn"
            onClick={() => (selecting ? exitSelect() : setSelecting(true))}
            aria-label="Выбрать сообщения"
            title={selecting ? 'Отменить выбор' : 'Выбрать сообщения'}
          >
            {selecting ? <IconClose /> : <IconCheckAll />}
          </button>
        )}
        <button
          className="icon-btn"
          onClick={() => $detailsOpen.set(!detailsOpen)}
          aria-label="Детали"
          aria-expanded={detailsOpen}
          title="Детали"
        >
          <IconInfo />
        </button>
        {detailsOpen && (
          <>
            <button
              className="pop-scrim"
              onClick={() => $detailsOpen.set(false)}
              aria-label="Закрыть панель"
              tabIndex={-1}
            />
            <DetailsPanel convId={convId} />
          </>
        )}
      </header>

      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        {hasMore && (
          <div className="chat-more muted">{loadingMore ? 'Загрузка…' : 'Ранние сообщения'}</div>
        )}
        {msgs.length === 0 && streamList.length === 0 && (
          <div className="chat-empty muted">Нет сообщений — напишите первым</div>
        )}
        {msgs.map((m, i) => {
          const prev = msgs[i - 1]
          const showDay =
            !prev ||
            !isSameDay(new Date(m.created_at ?? 0), new Date(prev.created_at ?? 0))
          const own = m.author_id != null && m.author_id === me?.id
          const canDel = own || canManage
          const isPicked = picked.has(m.id)
          const delBtn = canDel && !selecting && (
            <button
              className="msg-del"
              onClick={() => void onDeleteMessage(m)}
              title="Удалить"
              aria-label="Удалить сообщение"
            >
              <IconTrash size={15} />
            </button>
          )
          const check = selecting && (
            <span className="msg-check" aria-hidden="true">
              <IconCheck size={13} />
            </span>
          )
          const sched = m.parts.find(
            (p) => p.type === 'text' && typeof p.scheduled === 'object' && p.scheduled !== null,
          )?.scheduled as { kind?: string; title?: string } | undefined
          if (m.author_kind === 'system' || sched) {
            const parts = sched
              ? m.parts.map((p) =>
                  p.type === 'text'
                    ? { ...p, text: `⏰ «${sched.title || 'по расписанию'}»: ${p.text ?? ''}` }
                    : p,
                )
              : m.parts
            return (
              <div key={m.id}>
                {showDay && <div className="msg-day">{dayLabel(m.created_at)}</div>}
                <SwipeRow
                  enabled={canDel && !selecting}
                  onDelete={() => void onDeleteMessage(m)}
                >
                  <div
                    className={`msg sys${selecting ? ' sel' : ''}${isPicked ? ' picked' : ''}`}
                    onClick={selecting ? () => togglePick(m.id) : undefined}
                  >
                    {check}
                    <span className="sys-note muted">
                      <MessageParts
                        parts={parts}
                        convId={convId}
                        origin={conv?.origin}
                        mentions={mentionNames}
                      />
                    </span>
                    {delBtn}
                  </div>
                </SwipeRow>
              </div>
            )
          }
          return (
            <div key={m.id}>
              {showDay && <div className="msg-day">{dayLabel(m.created_at)}</div>}
              <SwipeRow
                enabled={canDel && !selecting}
                onDelete={() => void onDeleteMessage(m)}
              >
                <div
                  className={`msg${own ? ' own' : ''}${m.author_kind === 'assistant' ? ' bot' : ''}${selecting ? ' sel' : ''}${isPicked ? ' picked' : ''}`}
                  onClick={selecting ? () => togglePick(m.id) : undefined}
                >
                  {check}
                  {!own && conv?.kind === 'group' && (
                    <span className="avatar small">{initials(authorName(m.author_id))}</span>
                  )}
                  {own && delBtn}
                  <div className="bubble">
                    {!own && conv?.kind === 'group' && (
                      <div className="msg-author">{authorName(m.author_id)}</div>
                    )}
                    <MessageParts
                      parts={m.parts}
                      convId={convId}
                      origin={conv?.origin}
                      autoplayTts={m.id === ttsAutoplayId}
                      mentions={mentionNames}
                    />
                    <span className="msg-meta">
                      {fmtTime(m.created_at)}
                      {m.author_kind === 'assistant' && (
                        <CopyButton
                          text={m.parts
                            .filter((p) => p.type === 'text')
                            .map((p) => String(p.text ?? ''))
                            .join('\n\n')}
                        />
                      )}
                      {own && <span className="ticks">{readByAll(m) ? '✓✓' : '✓'}</span>}
                    </span>
                  </div>
                  {!own && delBtn}
                </div>
              </SwipeRow>
            </div>
          )
        })}

        {streamList.map((s) => (
          <div key={s.runId} className="msg bot stream">
            <div className="bubble">
              {s.tools.map((t) => (
                <ToolChip
                  key={t.tool_call_id}
                  name={t.name}
                  status={t.status}
                  progress={t.progress}
                />
              ))}
              {s.text ? <Markdown text={s.text} mentions={mentionNames} /> : null}
              <span className="msg-meta">
                {s.status === 'running' ? (
                  <span className="stream-cursor">▍</span>
                ) : s.status === 'failed' ? (
                  <span className="muted">ошибка</span>
                ) : null}
              </span>
            </div>
          </div>
        ))}

        {pending.map((p) => (
          <div key={p.client_msg_id} className="msg own pending">
            <div className="bubble">
              <Markdown text={p.text} mentions={mentionNames} />
              <span className="msg-meta">
                {p.failed ? (
                  <>
                    <button
                      className="mini-btn"
                      onClick={() => void retryPending(convId, p.client_msg_id)}
                      title="Повторить"
                    >
                      <IconRetry size={14} />
                    </button>
                    <button
                      className="mini-btn"
                      onClick={() => discardPending(convId, p.client_msg_id)}
                      title="Удалить"
                    >
                      <IconTrash size={14} />
                    </button>
                  </>
                ) : (
                  <span className="spinner" />
                )}
              </span>
            </div>
          </div>
        ))}
      </div>

      {showJump && (
        <button className="jump-down" onClick={jumpToLatest} aria-label="К последним">
          <IconDown />
        </button>
      )}

      {selecting ? (
        <div className="composer select-bar">
          <span className="select-count muted">Выбрано: {picked.size}</span>
          <button
            className="icon-btn"
            disabled={picked.size === 0 || actionBusy}
            onClick={() => setForwardOpen(true)}
            aria-label="Переслать"
            title="Переслать"
          >
            <IconForward />
          </button>
          <button
            className="icon-btn danger"
            disabled={picked.size === 0 || actionBusy}
            onClick={() => void onDeletePicked()}
            aria-label="Удалить выбранные"
            title="Удалить выбранные"
          >
            <IconTrash />
          </button>
        </div>
      ) : deleted ? (
        <div className="composer archived-notice">
          <span className="muted">Сессия удалена</span>
        </div>
      ) : archived ? (
        <div className="composer archived-notice">
          <span className="muted">Сессия в архиве</span>
          {canManage && (
            <button
              className="btn"
              onClick={() => void patchConversation(convId, { status: 'active' })}
            >
              Вернуть из архива
            </button>
          )}
        </div>
      ) : (
        <Composer convId={convId} />
      )}

      {forwardOpen && (
        <ForwardDialog
          convId={convId}
          messageIds={msgs.filter((m) => picked.has(m.id)).map((m) => m.id)}
          excludeUserIds={new Set((conv?.members ?? []).map((m) => m.user_id))}
          onClose={() => setForwardOpen(false)}
          onDone={onForwarded}
        />
      )}
    </div>
  )
}
