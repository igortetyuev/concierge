import {
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
} from 'react'
import { useParams } from 'react-router'

import {
  SIDEBAR_MAX,
  SIDEBAR_MIN,
  clampSidebar,
  loadSidebarWidth,
  saveSidebarWidth,
} from '../lib/sidebarWidth'
import ChatView from './ChatView'
import ConversationList from './ConversationList'
import MemoryView from './MemoryView'
import SettingsView from './SettingsView'
import AdminView from './admin/AdminView'
import { IconChat } from './icons'

const KEYBOARD_STEP = 16

export default function Shell({ view }: { view?: 'memory' | 'settings' | 'admin' }) {
  const { id } = useParams()
  const convId = id ? Number(id) : null
  const mainOpen = convId !== null || view !== undefined

  const [width, setWidth] = useState<number | null>(loadSidebarWidth)
  const shellRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLElement>(null)
  const drag = useRef<{ x: number; w: number } | null>(null)

  function apply(w: number): void {
    shellRef.current?.style.setProperty('--sidebar-w', `${w}px`)
  }

  function commit(w: number | null): void {
    setWidth(w)
    saveSidebarWidth(w)
    if (w === null) shellRef.current?.style.removeProperty('--sidebar-w')
  }

  function currentWidth(): number {
    return listRef.current?.getBoundingClientRect().width ?? width ?? 300
  }

  function onPointerDown(e: PointerEvent<HTMLDivElement>): void {
    if (!e.isPrimary || (e.pointerType === 'mouse' && e.button !== 0)) return
    drag.current = { x: e.clientX, w: currentWidth() }
    e.currentTarget.setPointerCapture(e.pointerId)
    document.body.classList.add('sidebar-resizing')
    e.preventDefault()
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>): void {
    const d = drag.current
    if (!d) return
    apply(clampSidebar(d.w + e.clientX - d.x))
  }

  function onPointerUp(): void {
    if (!drag.current) return
    drag.current = null
    document.body.classList.remove('sidebar-resizing')
    commit(clampSidebar(currentWidth()))
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>): void {
    const cur = clampSidebar(currentWidth())
    let next: number | null = null
    if (e.key === 'ArrowLeft') next = cur - KEYBOARD_STEP
    else if (e.key === 'ArrowRight') next = cur + KEYBOARD_STEP
    else if (e.key === 'Home') next = SIDEBAR_MIN
    else if (e.key === 'End') next = SIDEBAR_MAX
    else return
    e.preventDefault()
    commit(clampSidebar(next))
  }

  return (
    <div
      ref={shellRef}
      className={`shell${mainOpen ? ' has-chat' : ''}`}
      style={
        width !== null
          ? ({ '--sidebar-w': `${width}px` } as CSSProperties)
          : undefined
      }
    >
      <aside ref={listRef} className="shell-list">
        <ConversationList />
        <div
          className="shell-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Ширина списка сессий"
          tabIndex={0}
          aria-valuemin={SIDEBAR_MIN}
          aria-valuemax={SIDEBAR_MAX}
          aria-valuenow={width !== null ? clampSidebar(width) : undefined}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onDoubleClick={() => commit(null)}
          onKeyDown={onKeyDown}
        />
      </aside>
      <main className="shell-chat">
        {convId ? (
          <ChatView key={convId} convId={convId} />
        ) : view === 'memory' ? (
          <MemoryView />
        ) : view === 'settings' ? (
          <SettingsView />
        ) : view === 'admin' ? (
          <AdminView />
        ) : (
          <div className="chat-empty-state muted">
            <IconChat size={40} />
            <p>Выберите сессию или создайте новую</p>
          </div>
        )}
      </main>
    </div>
  )
}
