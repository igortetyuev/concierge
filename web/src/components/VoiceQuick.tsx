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
import { IconClose, IconMic, IconSend, IconTool } from './icons'

type Mode = 'tap' | 'hold'
type Phase = 'idle' | 'recording' | 'sending'

const DEFAULT_MAX_S = 120

function fmt(s: number): string {
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/** Big mic button: FAB on phones, header button + `v` hotkey on desktop. */
export default function VoiceQuick() {
  const navigate = useNavigate()
  const me = useStore($user)

  const [phase, setPhase] = useState<Phase>('idle')
  const [seconds, setSeconds] = useState(0)
  const [level, setLevel] = useState(0)
  const [mode, setMode] = useState<Mode>('tap')
  const [maxS, setMaxS] = useState(DEFAULT_MAX_S)
  const [notice, setNotice] = useState<string | null>(null)
  const [showCfg, setShowCfg] = useState(false)

  const recRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const discardRef = useRef(false)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const rafRef = useRef(0)
  const phaseRef = useRef(phase)
  phaseRef.current = phase

  // User settings (server-backed, localStorage as fallback for read-only roles).
  useEffect(() => {
    const localMode = localStorage.getItem('vq.mode')
    if (localMode === 'tap' || localMode === 'hold') setMode(localMode)
    const localMax = Number(localStorage.getItem('vq.max_s'))
    if (localMax > 0) setMaxS(localMax)
    apiClient
      .listUserSettings()
      .then((s) => {
        const m = s['voice_quick.mode']
        if (m === 'tap' || m === 'hold') setMode(m)
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

  // Timer + duration limit while recording.
  useEffect(() => {
    if (phase !== 'recording') return
    const t = setInterval(() => {
      setSeconds((s) => {
        if (s + 1 >= maxS) stop(true)
        return s + 1
      })
    }, 1000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, maxS])

  useEffect(
    () => () => {
      cancelAnimationFrame(rafRef.current)
      void audioCtxRef.current?.close().catch(() => {})
      recRef.current?.stop()
    },
    [],
  )

  function saveCfg(nextMode: Mode, nextMax: number) {
    localStorage.setItem('vq.mode', nextMode)
    localStorage.setItem('vq.max_s', String(nextMax))
    void apiClient.putUserSetting('voice_quick.mode', nextMode).catch(() => {})
    void apiClient.putUserSetting('voice_quick.max_seconds', nextMax).catch(() => {})
  }

  function startMeter(stream: MediaStream) {
    try {
      const Ctx = window.AudioContext
      const ctx = new Ctx()
      audioCtxRef.current = ctx
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 256
      ctx.createMediaStreamSource(stream).connect(analyser)
      const buf = new Uint8Array(analyser.fftSize)
      const tick = () => {
        analyser.getByteTimeDomainData(buf)
        let sum = 0
        for (const v of buf) sum += (v - 128) * (v - 128)
        setLevel(Math.min(1, Math.sqrt(sum / buf.length) / 40))
        rafRef.current = requestAnimationFrame(tick)
      }
      tick()
    } catch {
      /* level meter is cosmetic */
    }
  }

  function stopMeter() {
    cancelAnimationFrame(rafRef.current)
    void audioCtxRef.current?.close().catch(() => {})
    audioCtxRef.current = null
    setLevel(0)
  }

  async function start() {
    if (phaseRef.current !== 'idle') return
    const blocked = micBlockedReason()
    if (blocked) {
      setNotice(blocked)
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mime = pickAudioMime()
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
      discardRef.current = false
      chunksRef.current = []
      rec.ondataavailable = (e) => {
        if (e.data.size) chunksRef.current.push(e.data)
      }
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop())
        stopMeter()
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' })
        chunksRef.current = []
        if (discardRef.current || !blob.size) {
          setPhase('idle')
        } else {
          void upload(blob, rec.mimeType || 'audio/webm')
        }
      }
      recRef.current = rec
      setSeconds(0)
      setShowCfg(false)
      setPhase('recording')
      rec.start()
      startMeter(stream)
    } catch (err) {
      setNotice(micErrorMessage(err))
      setPhase('idle')
    }
  }

  function stop(send: boolean) {
    discardRef.current = !send
    recRef.current?.stop()
    recRef.current = null
  }

  function toggle() {
    if (phaseRef.current === 'recording') stop(true)
    else void start()
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
        navigate(`/c/${resp.conversation_id}`)
      }
      setPhase('idle')
    } catch {
      setNotice('Ошибка отправки')
      setPhase('idle')
    }
  }

  // `v` toggles (tap semantics) outside text fields; Escape cancels.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null
      const typing =
        t != null &&
        (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)
      if (e.key === 'Escape' && phase === 'recording') {
        stop(false)
        return
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key.toLowerCase() === 'v') toggle()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  function onClick() {
    if (mode === 'tap') toggle()
  }

  function onPointerDown(e: PointerEvent<HTMLButtonElement>) {
    if (mode !== 'hold') return
    e.currentTarget.setPointerCapture(e.pointerId)
    void start()
  }

  function onPointerUp() {
    if (mode === 'hold' && phaseRef.current === 'recording') stop(true)
  }

  const allowed =
    me?.permissions['*'] === true || me?.permissions['voice_quick_session'] === true
  if (!allowed) return null

  const micButton = (cls: string, iconSize: number) => (
    <button
      className={cls}
      title="Голосовая сессия (V)"
      aria-label="Голосовая сессия"
      onClick={onClick}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onContextMenu={(e) => e.preventDefault()}
    >
      <IconMic size={iconSize} />
    </button>
  )

  return (
    <>
      {micButton('icon-btn vq-head', 20)}
      {micButton('vq-fab', 26)}
      {phase !== 'idle' && (
        <div className="vq-overlay" role="dialog" aria-label="Голосовой запрос">
          <div className="vq-card">
            {phase === 'sending' ? (
              <div className="vq-status">
                <span className="spinner" /> Распознаю…
              </div>
            ) : (
              <>
                <div className="vq-level">
                  <span style={{ width: `${Math.round(level * 100)}%` }} />
                </div>
                <div className="vq-time">
                  {fmt(seconds)} <span className="muted">/ {maxS} с</span>
                </div>
                <div className="vq-actions">
                  <button
                    className="icon-btn"
                    onClick={() => stop(false)}
                    aria-label="Отмена"
                    title="Отмена"
                  >
                    <IconClose />
                  </button>
                  {mode === 'tap' ? (
                    <button className="btn primary" onClick={() => stop(true)}>
                      <IconSend size={15} /> Отправить
                    </button>
                  ) : (
                    <span className="muted vq-hint">
                      Отпустите кнопку — запись отправится
                    </span>
                  )}
                  <button
                    className="icon-btn"
                    onClick={() => setShowCfg((v) => !v)}
                    aria-label="Настройки записи"
                    title="Настройки записи"
                  >
                    <IconTool />
                  </button>
                </div>
                {showCfg && (
                  <div className="vq-cfg">
                    <label>
                      <span>Режим</span>
                      <select
                        value={mode}
                        onChange={(e) => {
                          const m = e.target.value as Mode
                          setMode(m)
                          saveCfg(m, maxS)
                        }}
                      >
                        <option value="tap">Нажатие — старт/стоп</option>
                        <option value="hold">Удерживать кнопку</option>
                      </select>
                    </label>
                    <label>
                      <span>Лимит, сек</span>
                      <input
                        type="number"
                        min={10}
                        max={600}
                        value={maxS}
                        onChange={(e) => {
                          const v = Math.max(
                            10,
                            Math.min(600, Number(e.target.value) || DEFAULT_MAX_S),
                          )
                          setMaxS(v)
                          saveCfg(mode, v)
                        }}
                      />
                    </label>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
      {notice && <div className="vq-notice">{notice}</div>}
    </>
  )
}
