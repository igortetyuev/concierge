import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
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
  discardPending,
  loadInitial,
  loadOlder,
  retryPending,
} from '../store/messages'
import type { PendingMessage, StreamState } from '../store/messages'
import { markLatestRead } from '../store/realtime'
import { $detailsOpen } from '../store/ui'
import Composer from './Composer'
import MessageParts, { ToolChip } from './MessageParts'
import Markdown from './Markdown'
import { IconBack, IconDown, IconInfo, IconRetry, IconTrash } from './icons'

const EMPTY_MSGS: MessageOut[] = []
const EMPTY_PENDING: PendingMessage[] = []
const EMPTY_STREAMS: Record<number, StreamState> = {}
const EMPTY_IDS: number[] = []
const EMPTY_READ: Record<number, number> = {}

export default function ChatView({ convId }: { convId: number }) {
  const me = useStore($user)
  const conv = useStore($conversations).find((c) => c.id === convId)
  const msgs = useStore($messages)[convId] ?? EMPTY_MSGS
  const pending = useStore($pending)[convId] ?? EMPTY_PENDING
  const streams = useStore($streams)[convId] ?? EMPTY_STREAMS
  const hasMore = useStore($hasMore)[convId] ?? false
  const loadingMore = useStore($loadingMore)[convId] ?? false
  const online = useStore($online)[convId] ?? EMPTY_IDS
  const readSeqs = useStore($readSeqs)[convId] ?? EMPTY_READ
  useStore($typing) // subscribe for typing expiry updates

  const scrollRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const pendingAdjust = useRef<number | null>(null)
  const [showJump, setShowJump] = useState(false)
  const [, setTick] = useState(0)

  useEffect(() => {
    $activeId.set(convId)
    void refreshConversation(convId)
    void loadInitial(convId).then(() => markLatestRead(convId))
    return () => $activeId.set(null)
  }, [convId])

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
    if (id == null) return 'Консьерж'
    return conv?.members.find((m) => m.user_id === id)?.display_name ?? 'Участник'
  }

  function readByAll(m: MessageOut): boolean {
    const others = (conv?.members ?? []).filter((x) => x.user_id !== me?.id)
    return others.length > 0 && others.every((x) => (readSeqs[x.user_id] ?? 0) >= m.seq)
  }

  const typing = typingUsers(convId).filter((id) => id !== me?.id)
  const onlineOthers = online.filter((id) => id !== me?.id)
  const streamList = Object.values(streams).sort((a, b) => a.runId - b.runId)
  const archived = conv?.status === 'archived'
  const canManage = conv != null && (conv.owner_id === me?.id || me?.permissions['*'] === true)

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
        <button
          className="icon-btn"
          onClick={() => $detailsOpen.set(!$detailsOpen.get())}
          aria-label="Детали"
          title="Детали"
        >
          <IconInfo />
        </button>
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
          if (m.author_kind === 'system') {
            return (
              <div key={m.id}>
                {showDay && <div className="msg-day">{dayLabel(m.created_at)}</div>}
                <div className="msg sys">
                  <span className="sys-note muted">
                    <MessageParts parts={m.parts} convId={convId} origin={conv?.origin} />
                  </span>
                </div>
              </div>
            )
          }
          return (
            <div key={m.id}>
              {showDay && <div className="msg-day">{dayLabel(m.created_at)}</div>}
              <div className={`msg${own ? ' own' : ''}${m.author_kind === 'assistant' ? ' bot' : ''}`}>
                {!own && conv?.kind === 'group' && (
                  <span className="avatar small">{initials(authorName(m.author_id))}</span>
                )}
                <div className="bubble">
                  {!own && conv?.kind === 'group' && m.author_kind !== 'assistant' && (
                    <div className="msg-author">{authorName(m.author_id)}</div>
                  )}
                  <MessageParts
                    parts={m.parts}
                    convId={convId}
                    origin={conv?.origin}
                    autoplayTts={m.id === ttsAutoplayId}
                  />
                  <span className="msg-meta">
                    {fmtTime(m.created_at)}
                    {own && <span className="ticks">{readByAll(m) ? '✓✓' : '✓'}</span>}
                  </span>
                </div>
              </div>
            </div>
          )
        })}

        {streamList.map((s) => (
          <div key={s.runId} className="msg bot stream">
            <div className="bubble">
              {s.tools.map((t) => (
                <ToolChip key={t.tool_call_id} name={t.name} status={t.status} />
              ))}
              {s.text ? <Markdown text={s.text} /> : null}
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
              <Markdown text={p.text} />
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

      {archived ? (
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
    </div>
  )
}
