import { useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

import { sendTyping } from '../lib/ws'
import { sendMessage } from '../store/messages'
import { IconSend } from './icons'

const TYPING_INTERVAL = 3000

export default function Composer({ convId }: { convId: number }) {
  const [text, setText] = useState('')
  const areaRef = useRef<HTMLTextAreaElement>(null)
  const lastTyping = useRef(0)

  function autoresize() {
    const el = areaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  function onChange(value: string) {
    setText(value)
    autoresize()
    const now = Date.now()
    if (value && now - lastTyping.current > TYPING_INTERVAL) {
      lastTyping.current = now
      sendTyping(convId)
    }
  }

  async function submit() {
    const value = text
    setText('')
    requestAnimationFrame(autoresize)
    await sendMessage(convId, value)
    areaRef.current?.focus()
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void submit()
    }
  }

  return (
    <div className="composer">
      <textarea
        ref={areaRef}
        rows={1}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Сообщение"
        aria-label="Сообщение"
      />
      <button
        className="icon-btn send"
        onClick={() => void submit()}
        disabled={!text.trim()}
        aria-label="Отправить"
      >
        <IconSend />
      </button>
    </div>
  )
}
