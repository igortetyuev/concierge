import { useState } from 'react'
import { Link } from 'react-router'
import { useStore } from '@nanostores/react'

import { $user } from '../../store/auth'
import { IconBack } from '../icons'
import AdminUsers from './AdminUsers'
import AdminRoles from './AdminRoles'
import AdminPrompt from './AdminPrompt'
import AdminProviders from './AdminProviders'
import AdminMcp from './AdminMcp'
import AdminCodeAgents from './AdminCodeAgents'
import AdminTelegram from './AdminTelegram'
import AdminJournal from './AdminJournal'

type TabId =
  | 'users'
  | 'roles'
  | 'prompt'
  | 'providers'
  | 'mcp'
  | 'agents'
  | 'telegram'
  | 'journal'

const TABS: { id: TabId; label: string; perm: string }[] = [
  { id: 'users', label: 'Пользователи', perm: 'manage_users' },
  { id: 'roles', label: 'Роли', perm: 'manage_users' },
  { id: 'prompt', label: 'Промпт', perm: 'manage_users' },
  { id: 'providers', label: 'Провайдеры', perm: 'manage_providers' },
  { id: 'mcp', label: 'MCP', perm: 'manage_mcp' },
  { id: 'agents', label: 'Код-агенты', perm: 'manage_code_agents' },
  { id: 'telegram', label: 'Telegram', perm: 'manage_users' },
  { id: 'journal', label: 'Журнал', perm: 'manage_users' },
]

export default function AdminView() {
  const me = useStore($user)
  const [tab, setTab] = useState<TabId>('users')
  const can = (perm: string) =>
    me?.permissions['*'] === true || me?.permissions[perm] === true
  const tabs = TABS.filter((t) => can(t.perm))
  const active = tabs.some((t) => t.id === tab) ? tab : (tabs[0]?.id ?? 'users')

  return (
    <div className="memory">
      <header className="chat-head">
        <Link to="/" className="icon-btn chat-back" aria-label="Назад">
          <IconBack />
        </Link>
        <div className="chat-head-text">
          <div className="chat-title">Администрирование</div>
          <div className="chat-sub muted">настройка системы</div>
        </div>
      </header>

      {tabs.length === 0 && (
        <div className="details-scroll">
          <div className="details-section muted">Нет прав администратора</div>
        </div>
      )}
      {tabs.length > 0 && (
        <div className="admin-tabs" role="tablist">
          {tabs.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={active === t.id}
              className={active === t.id ? 'active' : ''}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}

      <div className="details-scroll">
        {tabs.length > 0 && active === 'users' && <AdminUsers />}
        {active === 'roles' && <AdminRoles />}
        {active === 'prompt' && <AdminPrompt />}
        {active === 'providers' && <AdminProviders />}
        {active === 'mcp' && <AdminMcp />}
        {active === 'agents' && <AdminCodeAgents />}
        {active === 'telegram' && <AdminTelegram />}
        {active === 'journal' && <AdminJournal />}
      </div>
    </div>
  )
}
