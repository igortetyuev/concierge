import { atom } from 'nanostores'

import { apiClient } from '../lib/api'
import type { ConversationOut, MessageOut, UserRef } from '../lib/api'
import * as ws from '../lib/ws'
import { messageText } from './messages'
import { $user } from './auth'

export const $conversations = atom<ConversationOut[]>([])
export const $users = atom<UserRef[]>([])
export const $unread = atom<Record<number, number>>({})
export const $maxSeq = atom<Record<number, number>>({})
export const $readSeqs = atom<Record<number, Record<number, number>>>({})
export const $online = atom<Record<number, number[]>>({})
export const $typing = atom<Record<number, Record<number, number>>>({})
export const $previews = atom<Record<number, string>>({})
export const $activeId = atom<number | null>(null)
export const $listStatus = atom<'idle' | 'loading' | 'error'>('idle')
export const $tab = atom<'active' | 'archived' | 'deleted'>('active')

function byUpdatedAt(list: ConversationOut[]): ConversationOut[] {
  return [...list].sort((a, b) =>
    (b.updated_at ?? '').localeCompare(a.updated_at ?? ''),
  )
}

function patchRecord<V>(
  store: { get(): Record<number, V>; set(v: Record<number, V>): void },
  id: number,
  value: V,
): void {
  store.set({ ...store.get(), [id]: value })
}

function dropRecord<V>(
  store: { get(): Record<number, V>; set(v: Record<number, V>): void },
  id: number,
): void {
  const next = { ...store.get() }
  delete next[id]
  store.set(next)
}

function myId(): number | null {
  return $user.get()?.id ?? null
}

function recomputeUnread(convId: number): void {
  const unread = Math.max(
    0,
    ($maxSeq.get()[convId] ?? 0) - ($readSeqs.get()[convId]?.[myId() ?? -1] ?? 0),
  )
  patchRecord($unread, convId, unread)
}

export function upsertConversation(conv: Partial<ConversationOut> & { id: number }): void {
  const list = $conversations.get()
  const idx = list.findIndex((c) => c.id === conv.id)
  if (idx >= 0) {
    const merged = { ...list[idx], ...conv }
    const next = [...list]
    next[idx] = merged
    $conversations.set(byUpdatedAt(next))
    if (conv.members) syncReadSeqs(merged)
  } else if (conv.title !== undefined && conv.members) {
    $conversations.set(byUpdatedAt([...list, conv as ConversationOut]))
    syncReadSeqs(conv as ConversationOut)
    ws.subscribe([conv.id])
  }
}

function syncReadSeqs(conv: ConversationOut): void {
  const seqs: Record<number, number> = {}
  for (const m of conv.members) seqs[m.user_id] = m.last_read_seq
  patchRecord($readSeqs, conv.id, { ...($readSeqs.get()[conv.id] ?? {}), ...seqs })
  recomputeUnread(conv.id)
}

export async function loadConversations(): Promise<ConversationOut[]> {
  $listStatus.set('loading')
  try {
    const convs = await apiClient.listConversations('all')
    $conversations.set(byUpdatedAt(convs))
    for (const c of convs) syncReadSeqs(c)
    $listStatus.set('idle')
    void loadPreviews(convs)
    return convs
  } catch {
    $listStatus.set('error')
    return []
  }
}

/** Latest message per conversation → list preview + unread baseline. */
async function loadPreviews(convs: ConversationOut[]): Promise<void> {
  await Promise.all(convs.map((c) => refreshPreview(c.id)))
}

export async function refreshPreview(convId: number): Promise<void> {
  try {
    const rows = await apiClient.listMessages(convId, undefined, 1)
    const last = rows[rows.length - 1]
    patchRecord($maxSeq, convId, last?.seq ?? 0)
    patchRecord($previews, convId, last ? messageText(last.parts).slice(0, 120) : '')
    recomputeUnread(convId)
  } catch {
    /* preview is best-effort */
  }
}

export async function refreshConversation(id: number): Promise<void> {
  const conv = await apiClient.getConversation(id)
  upsertConversation(conv)
}

export async function createPersonal(): Promise<ConversationOut> {
  const conv = await apiClient.createConversation('personal', '', [])
  upsertConversation(conv)
  return conv
}

export async function createGroup(title: string, memberIds: number[]): Promise<ConversationOut> {
  const conv = await apiClient.createConversation('group', title, memberIds)
  upsertConversation(conv)
  return conv
}

export async function patchConversation(
  id: number,
  patch: { title?: string; reply_mode?: 'always' | 'mention'; status?: 'active' | 'archived' },
): Promise<void> {
  const conv = await apiClient.patchConversation(id, patch)
  upsertConversation(conv)
}

export async function addMember(id: number, userId: number): Promise<void> {
  const conv = await apiClient.addMembers(id, [userId])
  upsertConversation(conv)
}

export async function removeMember(id: number, userId: number): Promise<void> {
  const conv = await apiClient.removeMember(id, userId)
  upsertConversation(conv)
}

export function removeConversation(id: number): void {
  $conversations.set($conversations.get().filter((c) => c.id !== id))
  dropRecord($unread, id)
  dropRecord($maxSeq, id)
  dropRecord($readSeqs, id)
  dropRecord($online, id)
  dropRecord($previews, id)
}

export async function deleteConversation(id: number): Promise<void> {
  await apiClient.deleteConversation(id)
  removeConversation(id)
}

export async function loadUsers(): Promise<void> {
  $users.set(await apiClient.listUsers())
}

export function applyMessageToList(message: MessageOut): void {
  const convId = message.conversation_id
  patchRecord($maxSeq, convId, Math.max($maxSeq.get()[convId] ?? 0, message.seq))
  const text = messageText(message.parts)
  if (text) patchRecord($previews, convId, text.slice(0, 120))
  const conv = $conversations.get().find((c) => c.id === convId)
  if (conv) {
    upsertConversation({ id: convId, updated_at: message.created_at ?? conv.updated_at })
  }
  const me = myId()
  // The server bumps the author's own last_read_seq without a read.updated event.
  if (me != null && message.author_id === me) {
    setReadSeq(convId, me, message.seq)
  }
  recomputeUnread(convId)
}

export function setReadSeq(convId: number, userId: number, seq: number): void {
  patchRecord($readSeqs, convId, {
    ...($readSeqs.get()[convId] ?? {}),
    [userId]: seq,
  })
  recomputeUnread(convId)
}

export async function markRead(convId: number, seq: number): Promise<void> {
  const me = myId()
  if (me == null || seq <= 0 || seq <= ($readSeqs.get()[convId]?.[me] ?? 0)) return
  try {
    const res = await apiClient.markRead(convId, seq)
    setReadSeq(convId, me, res.last_read_seq)
  } catch {
    /* read marker is best-effort */
  }
}

export function setOnline(convId: number, userIds: number[]): void {
  patchRecord($online, convId, userIds)
}

export function setPresence(convId: number, userId: number, online: boolean): void {
  const cur = new Set($online.get()[convId] ?? [])
  if (online) cur.add(userId)
  else cur.delete(userId)
  patchRecord($online, convId, [...cur])
}

export function noteTyping(convId: number, userId: number): void {
  patchRecord($typing, convId, {
    ...($typing.get()[convId] ?? {}),
    [userId]: Date.now() + 4000,
  })
}

export function typingUsers(convId: number): number[] {
  const now = Date.now()
  return Object.entries($typing.get()[convId] ?? {})
    .filter(([, until]) => until > now)
    .map(([id]) => Number(id))
}

/** Display title: explicit title, else member names (group) / fallback. */
export function convTitle(conv: ConversationOut | undefined): string {
  if (!conv) return '…'
  if (conv.title) return conv.title
  if (conv.kind === 'group') {
    const names = conv.members
      .filter((m) => m.user_id !== myId())
      .map((m) => m.display_name)
    return names.length ? names.join(', ') : 'Группа'
  }
  return 'Новая сессия'
}
