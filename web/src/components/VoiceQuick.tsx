import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { useNavigate } from 'react-router'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import {
  audioExt,
  micBlockedReason,
  micErrorMessage,
  pickAudioMime,
} from '../lib/audio'
import { subscribe } from '../lib/ws'
import { $user } from '../store/auth'
import { refreshConversation } from '../store/conversations'
import { $composerFocus } from '../store/ui'
import { IconMic } from './icons'

type Phase = 'idle' | 'recording' | 'sending'

const DEFAULT_MAX_S = 120
const RECORD_MIN_MS = 400

/** Big mic button: FAB on phones, header button + `v` hotkey on desktop.
 * Hold to record, release to send — the button itself lights up. */
export default function VoiceQuick() {
  const navigate = useNavigate()
  const me = useStore($user)

  const [phase, setPhase] = useState<Phase>('idle')
  const [maxS, setMaxS] = useState(DEFAULT_MAX_S)
  const [notice, setNotice] = useState<string | null>(null)

  const recRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const discardRef = useRef(false)
  const abortRef = useRef(false)
  const pendingRef = useRef(false)
  const startedAtRef = useRef(0)
  const startByRef = useRef<'pointer' | 'key' | null>(null)
  const phaseRef = useRef(phase)
  phaseRef.current = phase

  // User settings (server-backed, localStorage as fallback for read-only roles).
  useEffect(() => {
    const localMax = Number(localStorage.getItem('vq.max_s'))
    if (localMax > 0) setMaxS(localMax)
    apiClient
      .listUserSettings()
      .then((s) => {
        const lim = Number(s['voice_quick.max_seconds'])
        if (Number.isFinite(lim) && lim > 0) setMaxS(lim)
      })
      .catch(() => {})
  }, [])

  // Auto-clear the notice toast.
  useEffect(() => {
    if (!notice) return
    const t = setTimeout(() => setNotice(null), 4000)
    return () => clearTimeout(t)
  }, [notice])

  // Duration limit while recording.
  useEffect(() => {
    if (phase !== 'recording') return
    const t = setInterval(() => {
      if (Date.now() - startedAtRef.current >= maxS * 1000) stop(true)
    }, 500)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, maxS])

  useEffect(
    () => () => {
      discardRef.current = true
      recRef.current?.stop()
      abortRef.current = true
    },
    [],
  )

  async function start(by: 'pointer' | 'key') {
    if (phaseRef.current !== 'idle' || pendingRef.current) return
    const blocked = micBlockedReason()
    if (blocked) {
      setNotice(blocked)
      return
    }
    abortRef.current = false
    discardRef.current = false
    startByRef.current = by
    pendingRef.current = true
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (abortRef.current) {
        // Released before getUserMedia resolved — don't record at all.
        stream.getTracks().forEach((t) => t.stop())
        return
      }
      const mime = pickAudioMime()
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
      chunksRef.current = []
      rec.ondataavailable = (e) => {
        if (e.data.size) chunksRef.current.push(e.data)
      }
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop())
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' })
        chunksRef.current = []
        const tooShort = Date.now() - startedAtRef.current < RECORD_MIN_MS
        if (discardRef.current || tooShort || !blob.size) {
          setPhase('idle')
        } else {
          void upload(blob, rec.mimeType || 'audio/webm')
        }
      }
      startedAtRef.current = Date.now()
      recRef.current = rec
      setPhase('recording')
      rec.start()
    } catch (err) {
      setNotice(micErrorMessage(err))
      setPhase('idle')
    } finally {
      pendingRef.current = false
    }
  }

  function stop(send: boolean) {
    if (recRef.current == null) {
      // Release raced the async start: abort it and drop the take.
      abortRef.current = true
      discardRef.current = !send
      return
    }
    discardRef.current = !send
    recRef.current.stop()
    recRef.current = null
  }

  async function upload(blob: Blob, mime: string) {
    setPhase('sending')
    try {
      const resp = await apiClient.voiceQuick(blob, `voice.${audioExt(mime)}`)
      if (resp.status === 'empty_transcript') {
        setNotice('Ничего не расслышал — попробуйте ещё раз')
        setPhase('idle')
        return
      }
      if (resp.conversation_id != null) {
        subscribe([resp.conversation_id])
        try {
          await refreshConversation(resp.conversation_id)
        } catch {
          /* list resync covers it */
        }
        $composerFocus.set(resp.conversation_id)
        navigate(`/c/${resp.conversation_id}`)
      }
      setPhase('idle')
    } catch {
      setNotice('Ошибка отправки')
      setPhase('idle')
    }
  }

  // `v` held outside text fields records; releasing sends. Escape cancels.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null
      const typing =
        t != null &&
        (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)
      if (e.key === 'Escape' && phaseRef.current === 'recording') {
        stop(false)
        return
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey || e.repeat) return
      if (e.key.toLowerCase() === 'v') void start('key')
    }
    function onKeyUp(e: KeyboardEvent) {
      if (
        e.key.toLowerCase() === 'v' &&
        phaseRef.current === 'recording' &&
        startByRef.current === 'key'
      )
        stop(true)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onKeyUp)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function onPointerDown(e: PointerEvent<HTMLButtonElement>) {
    e.currentTarget.setPointerCapture(e.pointerId)
    void start('pointer')
  }

  function onPointerUp() {
    // No phase gate: releasing while getUserMedia is still pending must abort it,
    // otherwise the recording would start after the finger is already up.
    if (startByRef.current === 'pointer' && phaseRef.current !== 'sending')
      stop(true)
  }

  function onPointerCancel() {
    if (startByRef.current === 'pointer' && phaseRef.current !== 'sending')
      stop(false)
  }

  const allowed =
    me?.permissions['*'] === true || me?.permissions['voice_quick_session'] === true
  if (!allowed) return null

  const micButton = (cls: string, iconSize: number) => (
    <button
      className={`mic-btn ${cls}${phase === 'recording' ? ' recording' : ''}`}
      title="Удерживайте для записи (V)"
      aria-label="Голосовая сессия"
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onContextMenu={(e) => e.preventDefault()}
    >
      <IconMic size={iconSize} />
    </button>
  )

  return (
    <>
      {micButton('icon-btn vq-head', 20)}
      {micButton('vq-fab', 26)}
      {phase === 'sending' && (
        <div className="vq-notice">
          <span className="spinner" /> Распознаю…
        </div>
      )}
      {notice && <div className="vq-notice">{notice}</div>}
    </>
  )
}
