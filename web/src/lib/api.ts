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
  assistant_name: string
  members: MemberOut[]
}

export interface SpeakerInfo {
  voice_id: number
  user_id: number | null
  name: string
  score: number
}

export interface MessagePart {
  type: string
  text?: string
  name?: string
  tool_call_id?: string
  status?: string
  speaker?: SpeakerInfo
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

export interface AttachmentOut {
  id: number
  sha256: string
  mime: string
  size: number
  name: string
  kind: 'image' | 'audio' | 'video' | 'document' | 'other'
  conversation_id: number | null
  uploaded_by: number
  status: string
  transcript?: string
  caption?: string
  text?: string
  duration_s?: number
  pages?: number
  error?: string
  speaker?: SpeakerInfo
  created_at: string | null
}

export interface VoiceQuickOut {
  status: 'ok' | 'empty_transcript' | 'stt_failed'
  conversation_id: number | null
  message_id: number | null
  transcript: string | null
}

export interface VoiceMessageOut {
  status: 'ok' | 'empty_transcript' | 'stt_failed'
  message: MessageOut | null
  transcript: string | null
}

export interface TelegramLinkOut {
  linked: boolean
  telegram_username: string | null
  telegram_user_id: number | null
  bot_username: string | null
}

export interface TelegramLinkCodeOut {
  code: string
  bot_username: string | null
  expires_at: string
}

export interface AdminTelegramLinkOut {
  user_id: number
  username: string
  telegram_user_id: number
  telegram_username: string | null
  linked_at: string | null
}

export interface AdminUserOut {
  id: number
  username: string
  display_name: string
  role_id: number
  role: string
  is_active: boolean
  prompt_fragment: string | null
}

export interface RoleOut {
  id: number
  name: string
  permissions: Record<string, unknown>
  prompt_fragment: string | null
}

export interface ProviderOut {
  id: number
  name: string
  kind: string
  base_url: string | null
  api_key_ref: string | null
  enabled: boolean
}

export interface BindingOut {
  id: number
  capability: string
  provider_id: number
  model: string
  params: Record<string, unknown>
  role_id: number | null
}

export interface NamedModelOut {
  id: number
  name: string
  provider_id: number
  model: string
  params: Record<string, unknown>
  system_prompt: string | null
  tools: string[]
  enabled: boolean
}

export interface McpServerOut {
  id: number
  name: string
  transport: string
  command: string | null
  url: string | null
  env_ref: string | null
  enabled: boolean
  allowed_tools: string[]
}

export interface RunOut {
  id: number
  conversation_id: number
  conversation_title: string
  status: string
  model: string | null
  usage: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface AuditEventOut {
  id: number
  at: string
  actor_id: number | null
  actor_name: string
  action: string
  target: string
  detail: Record<string, unknown>
  ip: string
}

export interface InviteOut {
  id: number
  token?: string
  role_id: number
  expires_at: string
  used_by: number | null
}

export interface TelegramBindingOut {
  chat_id: number
  thread_id: number
  kind: string
  conversation_id: number
  title: string
}

export interface TelegramOverviewOut {
  running: boolean
  enabled: boolean
  bot_username: string | null
  links: number
  bindings: TelegramBindingOut[]
}

export interface CodeAgentOut {
  id: number
  name: string
  ssh_host: string
  workdir: string
  args: Record<string, unknown>
  is_default: boolean
}

export interface CodeAgentProject {
  name: string
  path: string
  description?: string
}

export interface VoiceOut {
  id: number
  label: string
  user_id: number | null
  user_name: string | null
  authenticated: boolean
  sample_count: number
  sample_attachment_id: number | null
  last_heard_at: string | null
  created_at: string | null
}

export interface VoiceSampleOut {
  id: number
  attachment_id: number
  attachment_name: string
  score: number
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

/** Download an attachment and return a revocable object URL. */
export async function fetchAttachmentObjectUrl(id: number): Promise<string> {
  await ensureFreshToken()
  const headers = new Headers()
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const resp = await fetch(`${BASE}/attachments/${id}`, { headers })
  if (!resp.ok) throw new ApiError(resp.status, 'attachment fetch failed')
  return URL.createObjectURL(await resp.blob())
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

function patchFn<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
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

  listConversations: (status: 'active' | 'archived' | 'deleted' | 'all' = 'all') =>
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
  deleteConversation: (id: number) =>
    api<{ ok: boolean }>(`/conversations/${id}`, { method: 'DELETE' }),

  listMessages: (id: number, beforeSeq?: number, limit = 50) =>
    api<MessageOut[]>(
      `/conversations/${id}/messages?limit=${limit}${beforeSeq ? `&before_seq=${beforeSeq}` : ''}`,
    ),
  postMessage: (id: number, text: string, clientMsgId: string) =>
    post<MessageOut>(`/conversations/${id}/messages`, {
      parts: [{ type: 'text', text }],
      client_msg_id: clientMsgId,
    }),
  postMessageParts: (id: number, parts: MessagePart[], clientMsgId: string) =>
    post<MessageOut>(`/conversations/${id}/messages`, {
      parts,
      client_msg_id: clientMsgId,
    }),
  deleteMessage: (id: number, messageId: number) =>
    api<{ ok: boolean }>(`/conversations/${id}/messages/${messageId}`, {
      method: 'DELETE',
    }),
  forwardMessages: (id: number, userId: number, messageIds: number[]) =>
    post<{ conversation_id: number; forwarded: number }>(
      `/conversations/${id}/messages/forward`,
      { user_id: userId, message_ids: messageIds },
    ),
  markRead: (id: number, seq: number) =>
    post<{ last_read_seq: number }>(`/conversations/${id}/read`, { seq }),

  uploadAttachment: (file: File | Blob, name: string, conversationId?: number) => {
    const form = new FormData()
    form.append('file', file, name)
    if (conversationId != null) form.append('conversation_id', String(conversationId))
    return api<AttachmentOut>('/attachments', { method: 'POST', body: form })
  },
  retranscribe: (attachmentId: number) =>
    api<AttachmentOut>(`/attachments/${attachmentId}/retranscribe`, { method: 'POST' }),
  voiceQuick: (file: Blob, name: string) => {
    const form = new FormData()
    form.append('file', file, name)
    return api<VoiceQuickOut>('/conversations/voice-quick', { method: 'POST', body: form })
  },
  voiceMessage: (
    conversationId: number,
    file: Blob,
    name: string,
    text: string,
    attachmentIds: number[],
    clientMsgId: string,
  ) => {
    const form = new FormData()
    form.append('file', file, name)
    if (text) form.append('text', text)
    for (const id of attachmentIds) form.append('attachment_ids', String(id))
    form.append('client_msg_id', clientMsgId)
    return api<VoiceMessageOut>(`/conversations/${conversationId}/voice-message`, {
      method: 'POST',
      body: form,
    })
  },
  listUserSettings: () => api<Record<string, unknown>>('/settings/user'),
  putUserSetting: (key: string, value: unknown) =>
    api<{ ok: boolean }>('/settings/user', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, value }),
    }),
  search: (q: string, limit = 20) =>
    api<MessageOut[]>(`/search?q=${encodeURIComponent(q)}&limit=${limit}`),

  telegramLink: () => api<TelegramLinkOut>('/telegram/link'),
  telegramLinkCode: () => post<TelegramLinkCodeOut>('/telegram/link-code'),
  telegramUnlink: () =>
    api<{ ok: boolean }>('/telegram/link', { method: 'DELETE' }),

  adminTelegramLinks: () => api<AdminTelegramLinkOut[]>('/admin/telegram/links'),
  adminTelegramAddLink: (
    userId: number,
    telegramUserId: number,
    telegramUsername?: string,
  ) =>
    post<AdminTelegramLinkOut>('/admin/telegram/links', {
      user_id: userId,
      telegram_user_id: telegramUserId,
      telegram_username: telegramUsername || null,
    }),
  adminTelegramRemoveLink: (userId: number) =>
    api<{ ok: boolean }>(`/admin/telegram/links/${userId}`, { method: 'DELETE' }),
  adminTelegramOverview: () =>
    api<TelegramOverviewOut>('/admin/telegram/overview'),
  listAdminUsers: () => api<AdminUserOut[]>('/admin/users'),
  createAdminUser: (body: {
    username: string
    display_name: string
    password: string
    role_id: number
    prompt_fragment?: string
  }) => post<AdminUserOut>('/admin/users', body),
  patchAdminUser: (
    id: number,
    patch: Partial<{
      display_name: string
      password: string
      role_id: number
      is_active: boolean
      prompt_fragment: string | null
    }>,
  ) => patchFn<AdminUserOut>(`/admin/users/${id}`, patch),
  deleteAdminUser: (id: number) =>
    api<{ ok: boolean }>(`/admin/users/${id}`, { method: 'DELETE' }),

  listRoles: () => api<RoleOut[]>('/admin/roles'),
  createRole: (body: {
    name: string
    permissions: Record<string, unknown>
    prompt_fragment?: string
  }) => post<RoleOut>('/admin/roles', body),
  patchRole: (
    id: number,
    patch: Partial<{
      name: string
      permissions: Record<string, unknown>
      prompt_fragment: string | null
    }>,
  ) => patchFn<RoleOut>(`/admin/roles/${id}`, patch),
  deleteRole: (id: number) =>
    api<{ ok: boolean }>(`/admin/roles/${id}`, { method: 'DELETE' }),

  listProviders: () => api<ProviderOut[]>('/admin/providers'),
  createProvider: (body: {
    name: string
    kind: string
    base_url?: string
    api_key_ref?: string
    enabled?: boolean
  }) => post<ProviderOut>('/admin/providers', body),
  patchProvider: (id: number, patch: Partial<ProviderOut>) =>
    patchFn<ProviderOut>(`/admin/providers/${id}`, patch),
  deleteProvider: (id: number) =>
    api<{ ok: boolean }>(`/admin/providers/${id}`, { method: 'DELETE' }),
  listProviderModels: (id: number, task?: string) =>
    api<string[]>(
      `/admin/providers/${id}/models${task ? `?task=${task}` : ''}`,
    ),

  listBindings: () => api<BindingOut[]>('/admin/capability-bindings'),
  createBinding: (body: {
    capability: string
    provider_id: number
    model: string
    params?: Record<string, unknown>
    role_id?: number | null
  }) => post<BindingOut>('/admin/capability-bindings', body),
  patchBinding: (id: number, patch: Partial<BindingOut>) =>
    patchFn<BindingOut>(`/admin/capability-bindings/${id}`, patch),
  deleteBinding: (id: number) =>
    api<{ ok: boolean }>(`/admin/capability-bindings/${id}`, {
      method: 'DELETE',
    }),

  listNamedModelTools: () => api<string[]>('/admin/named-model-tools'),
  listNamedModels: () => api<NamedModelOut[]>('/admin/named-models'),
  createNamedModel: (body: {
    name: string
    provider_id: number
    model: string
    params?: Record<string, unknown>
    system_prompt?: string
    tools?: string[]
    enabled?: boolean
  }) => post<NamedModelOut>('/admin/named-models', body),
  patchNamedModel: (id: number, patch: Partial<NamedModelOut>) =>
    patchFn<NamedModelOut>(`/admin/named-models/${id}`, patch),
  deleteNamedModel: (id: number) =>
    api<{ ok: boolean }>(`/admin/named-models/${id}`, { method: 'DELETE' }),

  listMcpServers: () => api<McpServerOut[]>('/admin/mcp-servers'),
  createMcpServer: (body: {
    name: string
    transport: string
    command?: string
    url?: string
    env_ref?: string
    enabled?: boolean
    allowed_tools?: string[]
  }) => post<McpServerOut>('/admin/mcp-servers', body),
  patchMcpServer: (id: number, patch: Partial<McpServerOut>) =>
    patchFn<McpServerOut>(`/admin/mcp-servers/${id}`, patch),
  deleteMcpServer: (id: number) =>
    api<{ ok: boolean }>(`/admin/mcp-servers/${id}`, { method: 'DELETE' }),

  listRuns: (limit = 50, offset = 0) =>
    api<{ total: number; runs: RunOut[] }>(
      `/admin/runs?limit=${limit}&offset=${offset}`,
    ),
  promptPreview: (userId: number, conversationId?: number) =>
    api<{ prompt: string }>(
      `/admin/prompt-preview?user_id=${userId}${conversationId ? `&conversation_id=${conversationId}` : ''}`,
    ),
  listAudit: (limit = 100, offset = 0) =>
    api<{ total: number; events: AuditEventOut[] }>(
      `/admin/audit-log?limit=${limit}&offset=${offset}`,
    ),

  listVoices: () => api<VoiceOut[]>('/admin/voices'),
  patchVoice: (
    id: number,
    patch: { label?: string; user_id?: number | null; authenticated?: boolean },
  ) => patchFn<VoiceOut>(`/admin/voices/${id}`, patch),
  mergeVoice: (id: number, intoId: number) =>
    post<{ ok: boolean }>(`/admin/voices/${id}/merge`, { into_id: intoId }),
  deleteVoice: (id: number) =>
    api<{ ok: boolean }>(`/admin/voices/${id}`, { method: 'DELETE' }),
  listVoiceSamples: (id: number) =>
    api<VoiceSampleOut[]>(`/admin/voices/${id}/samples`),
  addVoiceSample: (id: number, attachmentId: number) =>
    post<VoiceSampleOut>(`/admin/voices/${id}/samples`, {
      attachment_id: attachmentId,
    }),
  deleteVoiceSample: (id: number, sampleId: number) =>
    api<{ ok: boolean }>(`/admin/voices/${id}/samples/${sampleId}`, {
      method: 'DELETE',
    }),

  listInvites: () => api<InviteOut[]>('/invites'),
  createInvite: (roleId: number) =>
    post<InviteOut>('/invites', { role_id: roleId }),

  listCodeAgents: () => api<CodeAgentOut[]>('/admin/code-agents'),
  createCodeAgent: (body: {
    name: string
    ssh_host: string
    workdir: string
    is_default?: boolean
  }) => post<CodeAgentOut>('/admin/code-agents', body),
  patchCodeAgent: (id: number, patch: Partial<CodeAgentOut>) =>
    api<CodeAgentOut>(`/admin/code-agents/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  deleteCodeAgent: (id: number) =>
    api<{ ok: boolean }>(`/admin/code-agents/${id}`, { method: 'DELETE' }),

  listGlobalSettings: () => api<Record<string, unknown>>('/settings/global'),
  putGlobalSetting: (key: string, value: unknown) =>
    api<{ ok: boolean }>('/settings/global', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, value }),
    }),

  listMemory: () => api<MemoryItemOut[]>('/memory'),
  addMemory: (text: string, scope: 'user' | 'family') =>
    post<MemoryItemOut>('/memory', { text, scope }),
  deleteMemory: (id: number) =>
    api<{ ok: boolean }>(`/memory/${id}`, { method: 'DELETE' }),
}
