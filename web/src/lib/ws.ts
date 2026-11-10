import { ApiError, ensureFreshToken, getAccessToken, reportAuthLost } from './api'

export interface WsEvent {
  type: string
  conversation_id?: number
  [key: string]: unknown
}

type EventHandler = (ev: WsEvent) => void

let socket: WebSocket | null = null
let eventHandler: EventHandler | null = null
let openHandler: (() => void) | null = null
let stopped = true
let retry = 0
let retryTimer: ReturnType<typeof setTimeout> | undefined

export function setHandlers(onEvent: EventHandler, onOpen: () => void): void {
  eventHandler = onEvent
  openHandler = onOpen
}

export function start(): void {
  if (!stopped) return
  stopped = false
  void connect()
}

export function stop(): void {
  stopped = true
  if (retryTimer) clearTimeout(retryTimer)
  socket?.close()
  socket = null
}

export function isOpen(): boolean {
  return socket?.readyState === WebSocket.OPEN
}

export function send(obj: Record<string, unknown>): void {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(obj))
}

export function subscribe(conversationIds: Iterable<number>): void {
  for (const id of conversationIds) {
    send({ type: 'subscribe', conversation_id: id })
  }
}

export function sendTyping(conversationId: number): void {
  send({ type: 'typing', conversation_id: conversationId })
}

async function connect(): Promise<void> {
  if (stopped) return
  try {
    await ensureFreshToken()
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      reportAuthLost()
      return
    }
    scheduleRetry()
    return
  }
  if (stopped) return
  const token = getAccessToken()
  if (!token) {
    scheduleRetry()
    return
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(
    `${proto}://${location.host}/api/ws?token=${encodeURIComponent(token)}`,
  )
  socket = ws
  ws.onopen = () => {
    retry = 0
    openHandler?.()
  }
  ws.onmessage = (e) => {
    try {
      eventHandler?.(JSON.parse(e.data as string) as WsEvent)
    } catch {
      /* malformed frame — ignore */
    }
  }
  ws.onclose = () => {
    if (socket === ws) socket = null
    if (!stopped) scheduleRetry()
  }
}

function scheduleRetry(): void {
  const delay = Math.min(30_000, 1000 * 2 ** retry)
  retry = Math.min(retry + 1, 10)
  retryTimer = setTimeout(() => void connect(), delay)
}

// Mobile browsers suspend the socket in background; reconnect promptly on return.
document.addEventListener('visibilitychange', () => {
  if (!stopped && document.visibilityState === 'visible' && !isOpen()) {
    retry = 0
    if (retryTimer) clearTimeout(retryTimer)
    void connect()
  }
})

