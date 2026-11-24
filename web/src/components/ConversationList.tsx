import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { useStore } from '@nanostores/react'

import { apiClient } from '../lib/api'
import type { ConversationOut, MessageOut } from '../lib/api'
import { fmtListDate, initials } from '../lib/format'
import { logout, $user } from '../store/auth'
import {
  $conversations,
  $listStatus,
  $previews,
  $tab,
  $unread,
  convTitle,
  createPersonal,
} from '../store/conversations'
import { messageText } from '../store/messages'
import { $composerFocus } from '../store/ui'
import NewGroupDialog from './NewGroupDialog'
import VoiceQuick from './VoiceQuick'
import { IconBook, IconChat, IconChevron, IconLogout, IconPlus, IconSearch, IconSettings, IconTool, IconUsers } from './icons'

const DAY_MS = 24 * 60 * 60 * 1000

interface ConvGroup {
  key: string
  label: string
  items: ConversationOut[]
}

function monthLabel(d: Date, nowYear: number): string {
  const month = d.toLocaleDateString('ru-RU', { month: 'long' })
  const name = month.charAt(0).toUpperCase() + month.slice(1)
  return d.getFullYear() === nowYear ? name : `${name} ${d.getFullYear()}`
}

/** Группировка списка (отсортированного по updated_at desc): сегодня,
 *  прошедшая неделя без сегодня, дальше календарные месяцы. */
function groupConvs(list: ConversationOut[]): ConvGroup[] {
  const now = new Date()
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const weekStart = todayStart - 7 * DAY_MS
  const groups: ConvGroup[] = []
  const byKey = new Map<string, ConvGroup>()
  for (const c of list) {
    const t = c.updated_at ? new Date(c.updated_at).getTime() : 0
    let key: string
    let label: string
    if (t >= todayStart) {
      key = 'today'
      label = 'Сегодня'
    } else if (t >= weekStart) {
      key = 'week'
      label = 'Прошлая неделя'
    } else {
      const d = new Date(t)
      key = `m-${d.getFullYear()}-${d.getMonth()}`
      label = monthLabel(d, now.getFullYear())
    }
    let g = byKey.get(key)
    if (!g) {
      g = { key, label, items: [] }
      byKey.set(key, g)
      groups.push(g)
    }
    g.items.push(c)
  }
  return groups
}

export default function ConversationList() {
  const navigate = useNavigate()
  const params = useParams()
  const activeId = params.id ? Number(params.id) : null
  const convs = useStore($conversations)
  const unread = useStore($unread)
  const previews = useStore($previews)
  const tab = useStore($tab)
  const status = useStore($listStatus)
  const me = useStore($user)

  const [query, setQuery] = useState('')
  const [results, setResults] = useState<MessageOut[] | null>(null)
  const [showNewGroup, setShowNewGroup] = useState(false)
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())
  const debounce = useRef<ReturnType<typeof setTimeout>>(undefined)

  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current)
    const q = query.trim()
    if (!q) return
    debounce.current = setTimeout(async () => {
      try {
        setResults(await apiClient.search(q))
      } catch {
        setResults([])
      }
    }, 300)
    return () => {
      if (debounce.current) clearTimeout(debounce.current)
    }
  }, [query])

  async function newPersonal() {
    const conv = await createPersonal()
    $composerFocus.set(conv.id)
    navigate(`/c/${conv.id}`)
  }

  const canSeeDeleted = me?.permissions['*'] === true
  const filtered = convs.filter((c) =>
    tab === 'deleted'
      ? canSeeDeleted && c.status === 'deleted'
      : tab === 'archived'
        ? c.status === 'archived'
        : c.status === 'active',
  )
  const convById = new Map(convs.map((c) => [c.id, c]))
  const groups = groupConvs(filtered)

  function toggleGroup(key: string) {
    setCollapsedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function renderConv(c: ConversationOut) {
    return (
      <Link
        key={c.id}
        to={`/c/${c.id}`}
        role="listitem"
        className={`conv${c.id === activeId ? ' active' : ''}`}
      >
        <span className="avatar">{c.kind === 'group' ? <IconUsers size={18} /> : initials(convTitle(c) || 'С')}</span>
        <span className="conv-body">
          <span className="conv-title">
            {c.kind === 'group' && <IconUsers size={13} className="conv-kind" />}
            {convTitle(c)}
          </span>
          <span className="conv-preview">{previews[c.id] ?? ''}</span>
        </span>
        <span className="conv-meta">
          <span className="conv-time">{fmtListDate(c.updated_at)}</span>
          {(unread[c.id] ?? 0) > 0 && <span className="badge">{unread[c.id]}</span>}
        </span>
      </Link>
    )
  }

  const isAdmin =
    me?.permissions['*'] === true ||
    ['manage_users', 'manage_providers', 'manage_mcp', 'manage_code_agents', 'manage_skills'].some(
      (p) => me?.permissions[p] === true,
    )

  return (
    <div className="convlist">
      <header className="convlist-head">
        <button
          className="icon-btn"
          title="Новая сессия"
          aria-label="Новая сессия"
          onClick={() => void newPersonal()}
        >
          <IconPlus />
        </button>
        <VoiceQuick />
        <button
          className="icon-btn"
          title="Новая группа"
          aria-label="Новая группа"
          onClick={() => setShowNewGroup(true)}
        >
          <IconUsers />
        </button>
        <Link
          to="/memory"
          className="icon-btn"
          title="Память"
          aria-label="Память"
        >
          <IconBook />
        </Link>
        {isAdmin && (
          <Link
            to="/admin"
            className="icon-btn"
            title="Администрирование"
            aria-label="Администрирование"
          >
            <IconTool />
          </Link>
        )}
      </header>

      <div className="convlist-search">
        <IconSearch size={16} className="search-icon" />
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            if (!e.target.value.trim()) setResults(null)
          }}
          placeholder="Поиск по сообщениям"
          aria-label="Поиск"
        />
      </div>

      {results === null ? (
        <>
          <div className="convlist-tabs" role="tablist">
            <button
              role="tab"
              aria-selected={tab === 'active'}
              className={tab === 'active' ? 'active' : ''}
              onClick={() => $tab.set('active')}
            >
              Активные
            </button>
            <button
              role="tab"
              aria-selected={tab === 'archived'}
              className={tab === 'archived' ? 'active' : ''}
              onClick={() => $tab.set('archived')}
            >
              Архив
            </button>
            {canSeeDeleted && (
              <button
                role="tab"
                aria-selected={tab === 'deleted'}
                className={tab === 'deleted' ? 'active' : ''}
                onClick={() => $tab.set('deleted')}
              >
                Удалённые
              </button>
            )}
          </div>
          <div className="convlist-scroll" role="list">
            {filtered.length === 0 && status === 'idle' && (
              <div className="convlist-empty muted">
                {tab === 'active'
                  ? 'Нет сессий — начните новую'
                  : tab === 'archived'
                    ? 'Архив пуст'
                    : 'Нет удалённых сессий'}
              </div>
            )}
            {filtered.length === 0 && status === 'loading' && (
              <div className="convlist-empty muted">Загрузка…</div>
            )}
            {groups.map((g) => {
              const collapsed = collapsedGroups.has(g.key)
              return (
                <div key={g.key}>
                  <button
                    className="convgroup-head"
                    aria-expanded={!collapsed}
                    onClick={() => toggleGroup(g.key)}
                  >
                    <IconChevron
                      size={14}
                      className={`convgroup-chev${collapsed ? ' collapsed' : ''}`}
                    />
                    <span>{g.label}</span>
                    <span className="convgroup-count">{g.items.length}</span>
                  </button>
                  {!collapsed && g.items.map(renderConv)}
                </div>
              )
            })}
          </div>
        </>
      ) : (
        <div className="convlist-scroll" role="list">
          <div className="convlist-empty muted">
            {results.length === 0 ? 'Ничего не найдено' : `Найдено: ${results.length}`}
          </div>
          {results.map((m) => (
            <Link key={m.id} to={`/c/${m.conversation_id}`} className="conv" role="listitem">
              <span className="avatar"><IconChat size={18} /></span>
              <span className="conv-body">
                <span className="conv-title">{convTitle(convById.get(m.conversation_id))}</span>
                <span className="conv-preview">{messageText(m.parts).slice(0, 120)}</span>
              </span>
              <span className="conv-meta">
                <span className="conv-time">{fmtListDate(m.created_at)}</span>
              </span>
            </Link>
          ))}
        </div>
      )}

      <footer className="convlist-foot">
        <span className="avatar small">{initials(me?.display_name ?? '?')}</span>
        <span className="convlist-me">{me?.display_name}</span>
        <Link
          to="/settings"
          className="icon-btn"
          title="Настройки"
          aria-label="Настройки"
        >
          <IconSettings />
        </Link>
        <button
          className="icon-btn"
          title="Выйти"
          aria-label="Выйти"
          onClick={() => void logout()}
        >
          <IconLogout />
        </button>
      </footer>

      {showNewGroup && <NewGroupDialog onClose={() => setShowNewGroup(false)} />}
    </div>
  )
}
