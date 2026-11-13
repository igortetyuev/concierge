import * as ws from '../lib/ws'
import { api } from '../lib/api'
import type { AttachmentOut, MessageOut } from '../lib/api'
import { setAttachment } from './attachments'
import {
  $activeId,
  $speakerOnline,
  applyMessageToList,
  loadConversations,
  markRead,
  noteTyping,
  refreshConversation,
  refreshPreview,
  setOnline,
  setPresence,
  setReadSeq,
  upsertConversation,
} from './conversations'
import {
  $messages,
  applyDelta,
  applyMessageCreated,
  applyMessageDeleted,
  applyRunStatus,
  applyTool,
  resyncTail,
} from './messages'

let connected = false
let pollTimer: ReturnType<typeof setInterval> | undefined
let probeTimer: ReturnType<typeof setInterval> | undefined

export function connectRealtime(): void {
  if (connected) return
  connected = true
  ws.setHandlers(handleEvent, onOpen)
  ws.start()
  // Belt & braces: events cover most updates; this catches anything the
  // realtime stream missed while visible.
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') void resync()
  }, 60_000)
  void probeSpeaker()
  probeTimer = setInterval(() => {
    if (document.visibilityState === 'visible') void probeSpeaker()
  }, 30_000)
}

export function disconnectRealtime(): void {
  connected = false
  if (pollTimer) clearInterval(pollTimer)
  if (probeTimer) clearInterval(probeTimer)
  ws.stop()
  $speakerOnline.set(false)
}

async function probeSpeaker(): Promise<void> {
  try {
    const { online } = await api<{ online: boolean }>('/speaker/status')
    $speakerOnline.set(online)
  } catch {
    $speakerOnline.set(false)
  }
}

async function resync(): Promise<void> {
  const convs = await loadConversations()
  ws.subscribe(convs.map((c) => c.id))
  const active = $activeId.get()
  if (active != null) {
    await resyncTail(active)
    void markLatestRead(active)
  }
}

function onOpen(): void {
  void resync()
}

export function markLatestRead(convId: number): void {
  // Import lazily to avoid a store cycle at module init.
  const seq = maxSeqOf(convId)
  if (seq > 0 && document.visibilityState === 'visible') void markRead(convId, seq)
}

function maxSeqOf(convId: number): number {
  const list = $messages.get()[convId]
  return list?.length ? list[list.length - 1].seq : 0
}

function handleEvent(ev: ws.WsEvent): void {
  const convId = ev.conversation_id
  switch (ev.type) {
    case 'hello':
      // handled by onOpen resync (same socket event order)
      break
    case 'subscribed':
      if (convId != null && Array.isArray(ev.online_users)) {
        setOnline(convId, ev.online_users as number[])
      }
      break
    case 'presence':
      if (convId != null) setPresence(convId, ev.user_id as number, ev.online as boolean)
      break
    case 'typing':
      if (convId != null) noteTyping(convId, ev.user_id as number)
      break
    case 'message.created': {
      const message = ev.message as MessageOut
      applyMessageCreated(message)
      applyMessageToList(message)
      if (convId != null && convId === $activeId.get()) markLatestRead(convId)
      break
    }
    case 'message.deleted':
      if (convId != null) {
        applyMessageDeleted(convId, Number(ev.message_id))
        void refreshPreview(convId)
      }
      break
    case 'messages.invalidate':
      // Stored parts were rewritten server-side (voice rename/merge) —
      // pull the tail again so open chats show fresh speaker names.
      if (convId != null) void resyncTail(convId)
      break
    case 'message.delta':
      if (convId != null) applyDelta(convId, ev.run_id as number, String(ev.text ?? ''))
      break
    case 'message.tool':
      if (convId != null) applyTool(convId, ev.run_id as number, ev)
      break
    case 'run.status':
      if (convId != null) {
        applyRunStatus(
          convId,
          ev.run_id as number,
          String(ev.status ?? 'running'),
          ev.model as string | undefined,
        )
      }
      break
    case 'conversation.updated': {
      const conv = ev.conversation as Record<string, unknown> | undefined
      if (conv && typeof conv.id === 'number') {
        upsertConversation(conv as Parameters<typeof upsertConversation>[0])
        // Partial payloads ({id,title}) miss members/read state — refetch.
        if (!('members' in conv)) void refreshConversation(conv.id)
      }
      break
    }
    case 'attachment.updated':
      setAttachment(ev.attachment as AttachmentOut)
      break
    case 'read.updated':
      if (convId != null) {
        setReadSeq(convId, ev.user_id as number, ev.last_read_seq as number)
      }
      break
    default:
      break
  }
}
