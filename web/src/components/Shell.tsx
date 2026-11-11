import { useParams } from 'react-router'
import { useStore } from '@nanostores/react'

import { $detailsOpen } from '../store/ui'
import ChatView from './ChatView'
import ConversationList from './ConversationList'
import DetailsPanel from './DetailsPanel'
import MemoryView from './MemoryView'
import SettingsView from './SettingsView'
import { IconChat } from './icons'

export default function Shell({ view }: { view?: 'memory' | 'settings' }) {
  const { id } = useParams()
  const convId = id ? Number(id) : null
  const detailsOpen = useStore($detailsOpen)
  const mainOpen = convId !== null || view !== undefined

  return (
    <div
      className={`shell${mainOpen ? ' has-chat' : ''}${detailsOpen ? ' show-details' : ''}`}
    >
      <aside className="shell-list">
        <ConversationList />
      </aside>
      <main className="shell-chat">
        {convId ? (
          <ChatView key={convId} convId={convId} />
        ) : view === 'memory' ? (
          <MemoryView />
        ) : view === 'settings' ? (
          <SettingsView />
        ) : (
          <div className="chat-empty-state muted">
            <IconChat size={40} />
            <p>Выберите сессию или создайте новую</p>
          </div>
        )}
      </main>
      {detailsOpen && (
        <button
          className="details-scrim"
          onClick={() => $detailsOpen.set(false)}
          aria-label="Закрыть панель"
          tabIndex={-1}
        />
      )}
      <aside className="shell-details">
        {convId ? <DetailsPanel convId={convId} /> : null}
      </aside>
    </div>
  )
}
