export function pickAudioMime(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  const candidates = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm', '']
  for (const m of candidates) {
    if (!m || MediaRecorder.isTypeSupported(m)) return m
  }
  return ''
}

export function audioExt(mime: string): string {
  return mime.includes('mp4') ? 'm4a' : 'webm'
}

/** Why recording can't start before we even ask, or null if it can. */
export function micBlockedReason(): string | null {
  if (!window.isSecureContext)
    return 'Микрофон недоступен: откройте приложение по HTTPS'
  if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices)
    return 'Запись не поддерживается браузером'
  return null
}

export function micErrorMessage(err: unknown): string {
  const name = err instanceof DOMException ? err.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError')
    return 'Нет доступа к микрофону — разрешите его в браузере'
  if (name === 'NotFoundError' || name === 'OverconstrainedError')
    return 'Микрофон не найден'
  return 'Не удалось начать запись'
}
