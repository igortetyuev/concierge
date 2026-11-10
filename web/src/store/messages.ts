import { atom } from 'nanostores'

import { apiClient } from '../lib/api'
import type { MessageOut, MessagePart } from '../lib/api'

export interface PendingMessage {
  client_msg_id: string
  text: string
  failed: boolean
}

export interface ToolState {
  name: string
  tool_call_id: string
  status: string
}

export interface StreamState {
  runId: number
  text: string
  tools: ToolState[]
  status: string
  model?: string
}

export const $messages = atom<Record<number, MessageOut[]>>({})
export const $hasMore = atom<Record<number, boolean>>({})
export const $loadingMore = atom<Record<number, boolean>>({})
export const $pending = atom<Record<number, PendingMessage[]>>({})
export const $streams = atom<Record<number, Record<number, StreamState>>>({})

function patch<T>(store: { get(): Record<number, T>; set(v: Record<number, T>): void }, id: number, value: T): void {
  store.set({ ...store.get(), [id]: value })
}

function mergeSorted(list: MessageOut[], incoming: MessageOut[]): MessageOut[] {
  const byId = new Map<number, MessageOut>()
  for (const m of [...list, ...incoming]) byId.set(m.id, m)
  return [...byId.values()].sort((a, b) => a.seq - b.seq || a.id - b.id)
}

export async function loadInitial(conversationId: number): Promise<void> {
  const rows = await apiClient.listMessages(conversationId, undefined, 50)
  patch($messages, conversationId, mergeSorted($messages.get()[conversationId] ?? [], rows))
  patch($hasMore, conversationId, rows.length >= 50)
}

/** Re-fetch the tail after a reconnect; keeps older loaded pages. */
export async function resyncTail(conversationId: number): Promise<void> {
  const rows = await apiClient.listMessages(conversationId, undefined, 50)
  patch($messages, conversationId, mergeSorted($messages.get()[conversationId] ?? [], rows))
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
  const convId = message.conversation_id
  patch(
    $messages,
    convId,
    mergeSorted($messages.get()[convId] ?? [], [message]),
  )
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

export async function sendMessage(conversationId: number, text: string): Promise<void> {
  const trimmed = text.trim()
  if (!trimmed) return
  const clientMsgId = crypto.randomUUID()
  const entry: PendingMessage = { client_msg_id: clientMsgId, text: trimmed, failed: false }
  patch($pending, conversationId, [...($pending.get()[conversationId] ?? []), entry])
  try {
    const msg = await apiClient.postMessage(conversationId, trimmed, clientMsgId)
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

export function applyTool(conversationId: number, runId: number, part: MessagePart): void {
  const s = streamEntry(conversationId, runId)
  const toolId = String(part.tool_call_id ?? '')
  const tools = s.tools.some((t) => t.tool_call_id === toolId)
    ? s.tools.map((t) =>
        t.tool_call_id === toolId
          ? { ...t, status: String(part.status ?? t.status) }
          : t,
      )
    : [
        ...s.tools,
        {
          name: String(part.name ?? 'tool'),
          tool_call_id: toolId,
          status: String(part.status ?? 'running'),
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
