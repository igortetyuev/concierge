import { useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router'
import { useStore } from '@nanostores/react'

import { $authStatus, initAuth } from './store/auth'
import { connectRealtime, disconnectRealtime } from './store/realtime'
import { loadConversations } from './store/conversations'
import LoginScreen from './components/LoginScreen'
import Shell from './components/Shell'

export default function App() {
  const status = useStore($authStatus)

  useEffect(() => {
    void initAuth()
  }, [])

  useEffect(() => {
    if (status !== 'authed') return
    void loadConversations()
    connectRealtime()
    return () => disconnectRealtime()
  }, [status])

  if (status === 'loading') {
    return <div className="splash">Консьерж</div>
  }
  if (status === 'anon') {
    return <LoginScreen />
  }
  return (
    <Routes>
      <Route path="/" element={<Shell />} />
      <Route path="/c/:id" element={<Shell />} />
      <Route path="/memory" element={<Shell view="memory" />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
