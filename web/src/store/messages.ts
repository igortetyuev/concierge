import { atom } from 'nanostores'

import { apiClient } from '../lib/api'
import type { MessageOut, MessagePart } from '../lib/api'
import { uuid } from '../lib/uuid'

export interface PendingMessage {
  client_msg_id: string
  text: string
  failed: boolean
}

export interface ToolState {
  name: string
  tool_call_id: string
  status: string
  progress?: string
}

export interface StreamState {
  runId: number
  text: string
  thinking: string
  tools: ToolState[]
  status: string
  model?: string
}

export const $messages = atom<Record<number, MessageOut[]>>({})
export const $hasMore = atom<Record<number, boolean>>({})
export const $loadingMore = atom<Record<number, boolean>>({})
export const $pending = atom<Record<number, PendingMessage[]>>({})
export const $streams = atom<Record<number, Record<number, StreamState>>>({})
/** conversation_id -> message id of the latest live tts reply (autoplay once). */
export const $ttsAutoplay = atom<Record<number, number>>({})
/** run_id of streams whose "рассуждение" details block the user opened; the
 * opened state carries over to the final message when the run completes. */
export const $openWorkRuns = atom<Set<number>>(new Set())

export function setRunOpen(runId: number, open: boolean): void {
  const next = new Set($openWorkRuns.get())
  if (open) next.add(runId)
  else next.delete(runId)
  $openWorkRuns.set(next)
}

function patch<T>(store: { get(): Record<number, T>; set(v: Record<number, T>): void }, id: number, value: T): void {
  store.set({ ...store.get(), [id]: value })
}

function mergeSorted(list: MessageOut[], incoming: MessageOut[]): MessageOut[] {
  const byId = new Map<number, MessageOut>()
  for (const m of [...list, ...incoming]) byId.set(m.id, m)
  return [...byId.values()].sort((a, b) => a.seq - b.seq || a.id - b.id)
}

/**
 * Merge the freshly fetched tail over the loaded list: rows inside the tail
 * window are authoritative (drops messages deleted elsewhere), rows outside
 * (older pages, post-snapshot arrivals) are kept.
 */
function mergeTail(loaded: MessageOut[], tail: MessageOut[]): MessageOut[] {
  if (!tail.length) return loaded
  const lo = tail[0].seq
  const hi = tail[tail.length - 1].seq
  return mergeSorted(
    tail,
    loaded.filter((m) => m.seq < lo || m.seq > hi),
  )
}

export async function loadInitial(conversationId: number): Promise<void> {
  const rows = await apiClient.listMessages(conversationId, undefined, 50)
  patch($messages, conversationId, mergeTail($messages.get()[conversationId] ?? [], rows))
  patch($hasMore, conversationId, rows.length >= 50)
}

/** Re-fetch the tail after a reconnect; keeps older loaded pages. */
export async function resyncTail(conversationId: number): Promise<void> {
  const rows = await apiClient.listMessages(conversationId, undefined, 50)
  patch($messages, conversationId, mergeTail($messages.get()[conversationId] ?? [], rows))
}

export async function loadOlder(conversationId: number): Promise<void> {
  const list = $messages.get()[conversationId]
  const first = list?.[0]
  if (!first || $loadingMore.get()[conversationId]) return
  patch($loadingMore, conversationId, true)
  try {
    const rows = await apiClient.listMessages(conversationId, first.seq, 50)
    patch($messages, conversationId, mergeSorted(rows, $messages.get()[conversationId] ?? []))
    patch($hasMore, conversationId, rows.length >= 50)
  } finally {
    patch($loadingMore, conversationId, false)
  }
}

export function applyMessageCreated(message: MessageOut): void {
  if (message.hidden) return
  const convId = message.conversation_id
  patch(
    $messages,
    convId,
    mergeSorted($messages.get()[convId] ?? [], [message]),
  )
  if (message.parts.some((p) => p.tts === true)) {
    patch($ttsAutoplay, convId, message.id)
  }
  if (message.client_msg_id) {
    const pending = ($pending.get()[convId] ?? []).filter(
      (p) => p.client_msg_id !== message.client_msg_id,
    )
    patch($pending, convId, pending)
  }
  if (message.run_id != null) {
    const streams = { ...($streams.get()[convId] ?? {}) }
    delete streams[message.run_id]
    patch($streams, convId, streams)
  }
}

/** Replace a stored message in place (e.g. a choice part got answered). */
export function applyMessageUpdated(message: MessageOut): void {
  const list = $messages.get()[message.conversation_id]
  if (!list?.some((m) => m.id === message.id)) return
  patch(
    $messages,
    message.conversation_id,
    list.map((m) => (m.id === message.id ? message : m)),
  )
}

export function applyMessageDeleted(conversationId: number, messageId: number): void {
  const list = $messages.get()[conversationId]
  if (!list?.some((m) => m.id === messageId)) return
  patch(
    $messages,
    conversationId,
    list.filter((m) => m.id !== messageId),
  )
}

export async function deleteMessage(conversationId: number, messageId: number): Promise<void> {
  await apiClient.deleteMessage(conversationId, messageId)
  applyMessageDeleted(conversationId, messageId)
}

export async function sendMessage(conversationId: number, text: string): Promise<void> {
  const trimmed = text.trim()
  if (!trimmed) return
  await sendParts(conversationId, [{ type: 'text', text: trimmed }], trimmed)
}

export async function sendParts(
  conversationId: number,
  parts: MessagePart[],
  previewText: string,
): Promise<void> {
  if (!parts.length) return
  const clientMsgId = uuid()
  const entry: PendingMessage = { client_msg_id: clientMsgId, text: previewText, failed: false }
  patch($pending, conversationId, [...($pending.get()[conversationId] ?? []), entry])
  try {
    const msg = await apiClient.postMessageParts(conversationId, parts, clientMsgId)
    applyMessageCreated(msg)
  } catch {
    patch(
      $pending,
      conversationId,
      ($pending.get()[conversationId] ?? []).map((p) =>
        p.client_msg_id === clientMsgId ? { ...p, failed: true } : p,
      ),
    )
  }
}

export async function retryPending(conversationId: number, clientMsgId: string): Promise<void> {
  const entry = ($pending.get()[conversationId] ?? []).find(
    (p) => p.client_msg_id === clientMsgId,
  )
  if (!entry) return
  patch(
    $pending,
    conversationId,
    ($pending.get()[conversationId] ?? []).map((p) =>
      p.client_msg_id === clientMsgId ? { ...p, failed: false } : p,
    ),
  )
  try {
    const msg = await apiClient.postMessage(conversationId, entry.text, clientMsgId)
    applyMessageCreated(msg)
  } catch {
    patch(
      $pending,
      conversationId,
      ($pending.get()[conversationId] ?? []).map((p) =>
        p.client_msg_id === clientMsgId ? { ...p, failed: true } : p,
      ),
    )
  }
}

export function discardPending(conversationId: number, clientMsgId: string): void {
  patch(
    $pending,
    conversationId,
    ($pending.get()[conversationId] ?? []).filter(
      (p) => p.client_msg_id !== clientMsgId,
    ),
  )
}

function streamEntry(conversationId: number, runId: number): StreamState {
  return (
    $streams.get()[conversationId]?.[runId] ?? {
      runId,
      text: '',
      thinking: '',
      tools: [],
      status: 'running',
    }
  )
}

function putStream(conversationId: number, entry: StreamState): void {
  patch($streams, conversationId, {
    ...($streams.get()[conversationId] ?? {}),
    [entry.runId]: entry,
  })
}

export function applyDelta(conversationId: number, runId: number, text: string): void {
  const s = streamEntry(conversationId, runId)
  putStream(conversationId, { ...s, text: s.text + text })
}

/** A run may emit several thinking blocks; `start` marks a new one, and the
 * blocks join into a single string separated by a blank line. */
export function applyThinking(
  conversationId: number,
  runId: number,
  text: string,
  start: boolean,
): void {
  const s = streamEntry(conversationId, runId)
  const sep = start && s.thinking ? '\n\n' : ''
  if (!sep && !text) return
  putStream(conversationId, { ...s, thinking: s.thinking + sep + text })
}

export function applyTool(conversationId: number, runId: number, part: MessagePart): void {
  const s = streamEntry(conversationId, runId)
  const toolId = String(part.tool_call_id ?? '')
  const tools = s.tools.some((t) => t.tool_call_id === toolId)
    ? s.tools.map((t) =>
        t.tool_call_id === toolId
          ? {
              ...t,
              status: String(part.status ?? t.status),
              progress:
                part.progress != null ? String(part.progress) : t.progress,
            }
          : t,
      )
    : [
        ...s.tools,
        {
          name: String(part.name ?? 'tool'),
          tool_call_id: toolId,
          status: String(part.status ?? 'running'),
          progress:
            part.progress != null ? String(part.progress) : undefined,
        },
      ]
  putStream(conversationId, { ...s, tools })
}

export function applyRunStatus(
  conversationId: number,
  runId: number,
  status: string,
  model?: string,
): void {
  const s = streamEntry(conversationId, runId)
  putStream(conversationId, { ...s, status, model: model ?? s.model })
}

/** Part helper: extract the readable text of a message. */
export function messageText(parts: MessagePart[]): string {
  return parts
    .filter((p) => p.type === 'text')
    .map((p) => String(p.text ?? ''))
    .join('\n')
}

export function messageToolParts(parts: MessagePart[]): MessagePart[] {
  return parts.filter((p) => p.type === 'tool')
}
