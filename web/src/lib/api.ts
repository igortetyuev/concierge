export interface UserOut {
  id: number
  username: string
  display_name: string
  role: string
  permissions: Record<string, unknown>
}

export interface UserRef {
  id: number
  username: string
  display_name: string
}

export interface MemberOut {
  user_id: number
  username: string
  display_name: string
  joined_at: string | null
  last_read_seq: number
}

export interface ConversationOut {
  id: number
  kind: string
  title: string
  owner_id: number
  status: string
  reply_mode: string
  origin: string
  summary: string | null
  created_at: string | null
  updated_at: string | null
  archived_at: string | null
  members: MemberOut[]
}

export interface MessagePart {
  type: string
  text?: string
  name?: string
  tool_call_id?: string
  status?: string
  [key: string]: unknown
}

export interface MessageOut {
  id: number
  conversation_id: number
  seq: number
  author_kind: string
  author_id: number | null
  parts: MessagePart[]
  run_id: number | null
  source_channel: string
  client_msg_id: string | null
  created_at: string | null
}

export interface MemoryItemOut {
  id: number
  scope: 'user' | 'family'
  owner_id: number | null
  text: string
  kind: string
  source_message_id: number | null
  created_at: string | null
}

interface TokenOut {
  access_token: string
  token_type: string
  expires_in: number
  user: UserOut
}

const BASE = '/api'

let accessToken: string | null = null
let tokenExpiresAt = 0
let authLostHandler: (() => void) | null = null
let refreshing: Promise<TokenOut> | null = null

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export function setAccessToken(token: string | null, expiresIn = 0): void {
  accessToken = token
  tokenExpiresAt = token ? Date.now() + expiresIn * 1000 : 0
}

export function getAccessToken(): string | null {
  return accessToken
}

export function onAuthLost(fn: () => void): void {
  authLostHandler = fn
}

export function reportAuthLost(): void {
  setAccessToken(null)
  authLostHandler?.()
}

export async function refreshToken(): Promise<TokenOut> {
  refreshing ??= fetch(`${BASE}/auth/refresh`, { method: 'POST' })
    .then(async (resp) => {
      if (!resp.ok) throw new ApiError(resp.status, 'refresh failed')
      const data = (await resp.json()) as TokenOut
      setAccessToken(data.access_token, data.expires_in)
      return data
    })
    .finally(() => {
      refreshing = null
    })
  return refreshing
}

/** Ensure the access token is fresh. Throws ApiError on auth failure. */
export async function ensureFreshToken(): Promise<void> {
  if (accessToken && Date.now() <= tokenExpiresAt - 30_000) return
  await refreshToken()
}

async function rawFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers)
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  return fetch(`${BASE}${path}`, { ...init, headers })
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let resp = await rawFetch(path, init)
  if (resp.status === 401 && accessToken) {
    try {
      await ensureFreshToken()
      resp = await rawFetch(path, init)
    } catch {
      /* fall through to the 401 handling below */
    }
  }
  if (resp.status === 401) {
    reportAuthLost()
    throw new ApiError(401, 'unauthorized')
  }
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`
    try {
      const body = (await resp.json()) as { detail?: unknown }
      if (body.detail) message = String(body.detail)
    } catch {
      /* keep the HTTP status message */
    }
    throw new ApiError(resp.status, message)
  }
  return resp.json() as Promise<T>
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function deviceName(): string {
  const ua = navigator.userAgent
  const os = /iphone|ipad/i.test(ua)
    ? 'iOS'
    : /android/i.test(ua)
      ? 'Android'
      : /mac/i.test(ua)
        ? 'macOS'
        : /windows/i.test(ua)
          ? 'Windows'
          : /linux/i.test(ua)
            ? 'Linux'
            : 'web'
  return `PWA ${os}`.slice(0, 128)
}

export const apiClient = {
  login: (username: string, password: string) =>
    post<TokenOut>('/auth/login', {
      username,
      password,
      device_name: deviceName(),
    }),

  register: (token: string, username: string, displayName: string, password: string) =>
    post<TokenOut>(`/auth/register?token=${encodeURIComponent(token)}`, {
      username,
      display_name: displayName,
      password,
      device_name: deviceName(),
    }),

  logout: () => post<{ ok: boolean }>('/auth/logout'),
  me: () => api<UserOut>('/auth/me'),
  listUsers: () => api<UserRef[]>('/users'),

  listConversations: (status: 'active' | 'archived' | 'all' = 'all') =>
    api<ConversationOut[]>(`/conversations?status=${status}`),
  getConversation: (id: number) => api<ConversationOut>(`/conversations/${id}`),
  createConversation: (kind: 'personal' | 'group', title: string, memberIds: number[]) =>
    post<ConversationOut>('/conversations', {
      kind,
      title,
      member_ids: memberIds,
    }),
  patchConversation: (
    id: number,
    patch: { title?: string; reply_mode?: 'always' | 'mention'; status?: 'active' | 'archived' },
  ) =>
    api<ConversationOut>(`/conversations/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  addMembers: (id: number, userIds: number[]) =>
    post<ConversationOut>(`/conversations/${id}/members`, { user_ids: userIds }),
  removeMember: (id: number, userId: number) =>
    api<ConversationOut>(`/conversations/${id}/members/${userId}`, { method: 'DELETE' }),

  listMessages: (id: number, beforeSeq?: number, limit = 50) =>
    api<MessageOut[]>(
      `/conversations/${id}/messages?limit=${limit}${beforeSeq ? `&before_seq=${beforeSeq}` : ''}`,
    ),
  postMessage: (id: number, text: string, clientMsgId: string) =>
    post<MessageOut>(`/conversations/${id}/messages`, {
      parts: [{ type: 'text', text }],
      client_msg_id: clientMsgId,
    }),
  markRead: (id: number, seq: number) =>
    post<{ last_read_seq: number }>(`/conversations/${id}/read`, { seq }),
  search: (q: string, limit = 20) =>
    api<MessageOut[]>(`/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  listMemory: () => api<MemoryItemOut[]>('/memory'),
  addMemory: (text: string, scope: 'user' | 'family') =>
    post<MemoryItemOut>('/memory', { text, scope }),
  deleteMemory: (id: number) =>
    api<{ ok: boolean }>(`/memory/${id}`, { method: 'DELETE' }),
}
