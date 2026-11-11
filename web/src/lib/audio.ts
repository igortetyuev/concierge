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
