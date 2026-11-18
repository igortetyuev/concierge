import { useParams } from 'react-router'

import ChatView from './ChatView'
import ConversationList from './ConversationList'
import MemoryView from './MemoryView'
import SettingsView from './SettingsView'
import AdminView from './admin/AdminView'
import { IconChat } from './icons'

export default function Shell({ view }: { view?: 'memory' | 'settings' | 'admin' }) {
  const { id } = useParams()
  const convId = id ? Number(id) : null
  const mainOpen = convId !== null || view !== undefined

  return (
    <div className={`shell${mainOpen ? ' has-chat' : ''}`}>
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
