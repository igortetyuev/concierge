import { useEffect, useState } from 'react'

import { apiClient } from '../../lib/api'
import type {
  BindingOut,
  NamedModelOut,
  ProviderOut,
  RoleOut,
} from '../../lib/api'
import { IconEdit, IconPlus, IconTrash } from '../icons'
import {
  BindingDialog,
  NamedModelDialog,
  ProviderDialog,
} from './AdminProviderDialogs'

type Dialog =
  | { kind: 'provider'; item: ProviderOut | null }
  | { kind: 'binding'; item: BindingOut | null }
  | { kind: 'named'; item: NamedModelOut | null }

export default function AdminProviders() {
  const [providers, setProviders] = useState<ProviderOut[] | null>(null)
  const [bindings, setBindings] = useState<BindingOut[]>([])
  const [named, setNamed] = useState<NamedModelOut[]>([])
  const [namedToolNames, setNamedToolNames] = useState<string[]>([])
  const [roles, setRoles] = useState<RoleOut[]>([])
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [ps, bs, ns, rs, ts] = await Promise.all([
        apiClient.listProviders(),
        apiClient.listBindings(),
        apiClient.listNamedModels(),
        apiClient.listRoles(),
        apiClient.listNamedModelTools(),
      ])
      setProviders(ps)
      setBindings(bs)
      setNamed(ns)
      setRoles(rs)
      setNamedToolNames(ts)
    } catch {
      setError('Не удалось загрузить провайдеров')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  function providerName(id: number): string {
    return providers?.find((p) => p.id === id)?.name ?? `#${id}`
  }

  function roleName(id: number | null): string {
    if (id === null) return 'все роли'
    return roles.find((r) => r.id === id)?.name ?? `#${id}`
  }

  async function toggleProvider(p: ProviderOut) {
    try {
      await apiClient.patchProvider(p.id, { enabled: !p.enabled })
      await reload()
    } catch {
      setError('Не удалось сохранить')
    }
  }

  async function removeProvider(id: number) {
    try {
      await apiClient.deleteProvider(id)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить')
    }
  }

  async function removeBinding(id: number) {
    try {
      await apiClient.deleteBinding(id)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить')
    }
  }

  async function toggleNamed(m: NamedModelOut) {
    try {
      await apiClient.patchNamedModel(m.id, { enabled: !m.enabled })
      await reload()
    } catch {
      setError('Не удалось сохранить')
    }
  }

  async function removeNamed(id: number) {
    try {
      await apiClient.deleteNamedModel(id)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось удалить')
    }
  }

  return (
    <section className="details-section">
      <div className="admin-section-head">
        <h3>Провайдеры моделей</h3>
        <button
          className="icon-btn"
          title="Добавить провайдера"
          aria-label="Добавить провайдера"
          onClick={() => setDialog({ kind: 'provider', item: null })}
        >
          <IconPlus size={16} />
        </button>
      </div>
      {providers === null ? (
        <div className="muted">Загрузка…</div>
      ) : (
        providers.map((p) => (
          <div key={p.id} className="memory-item">
            <span className="memory-item-text">
              {p.name} <span className="muted">· {p.kind}</span>
              {p.base_url && <span className="muted"> · {p.base_url}</span>}
              {p.api_key_ref && (
                <span className="muted">
                  {' '}
                  · key: {p.api_key_ref.slice(0, 10)}
                  {p.api_key_ref.length > 10 ? '…' : ''}
                </span>
              )}
              {!p.enabled && <span className="muted"> · выключен</span>}
            </span>
            <label className="settings-toggle">
              <input
                type="checkbox"
                checked={p.enabled}
                onChange={() => void toggleProvider(p)}
              />
            </label>
            <button
              className="icon-btn"
              title="Редактировать провайдера"
              aria-label="Редактировать провайдера"
              onClick={() => setDialog({ kind: 'provider', item: p })}
            >
              <IconEdit size={16} />
            </button>
            <button
              className="icon-btn"
              title="Удалить провайдера"
              aria-label="Удалить провайдера"
              onClick={() => void removeProvider(p.id)}
            >
              <IconTrash size={16} />
            </button>
          </div>
        ))
      )}

      <div className="admin-section-head">
        <h3>Назначение capability → модель</h3>
        <button
          className="icon-btn"
          title="Добавить binding"
          aria-label="Добавить binding"
          onClick={() => setDialog({ kind: 'binding', item: null })}
        >
          <IconPlus size={16} />
        </button>
      </div>
      {bindings.map((b) => (
        <div key={b.id} className="memory-item">
          <span className="memory-item-text">
            {b.capability} → {providerName(b.provider_id)}/{b.model}
            {b.role_id !== null && (
              <span className="muted"> · {roleName(b.role_id)}</span>
            )}
          </span>
          <button
            className="icon-btn"
            title="Редактировать binding"
            aria-label="Редактировать binding"
            onClick={() => setDialog({ kind: 'binding', item: b })}
          >
            <IconEdit size={16} />
          </button>
          <button
            className="icon-btn"
            title="Удалить binding"
            aria-label="Удалить binding"
            onClick={() => void removeBinding(b.id)}
          >
            <IconTrash size={16} />
          </button>
        </div>
      ))}

      <div className="admin-section-head">
        <h3>Именованные модели («спроси …»)</h3>
        <button
          className="icon-btn"
          title="Добавить модель"
          aria-label="Добавить модель"
          onClick={() => setDialog({ kind: 'named', item: null })}
        >
          <IconPlus size={16} />
        </button>
      </div>
      {named.map((m) => (
        <div key={m.id} className="memory-item">
          <span className="memory-item-text">
            {m.name} → {providerName(m.provider_id)}/{m.model}
            {m.system_prompt && (
              <span className="muted"> · {m.system_prompt}</span>
            )}
            {m.tools.length > 0 && (
              <span className="muted"> · tools: {m.tools.join(', ')}</span>
            )}
            {!m.enabled && <span className="muted"> · выключена</span>}
          </span>
          <label className="settings-toggle">
            <input
              type="checkbox"
              checked={m.enabled}
              onChange={() => void toggleNamed(m)}
            />
          </label>
          <button
            className="icon-btn"
            title="Редактировать модель"
            aria-label="Редактировать модель"
            onClick={() => setDialog({ kind: 'named', item: m })}
          >
            <IconEdit size={16} />
          </button>
          <button
            className="icon-btn"
            title="Удалить модель"
            aria-label="Удалить модель"
            onClick={() => void removeNamed(m.id)}
          >
            <IconTrash size={16} />
          </button>
        </div>
      ))}
      {error && <div className="auth-error">{error}</div>}

      {dialog?.kind === 'provider' && (
        <ProviderDialog
          item={dialog.item}
          onClose={() => setDialog(null)}
          onSaved={reload}
        />
      )}
      {dialog?.kind === 'binding' && (
        <BindingDialog
          item={dialog.item}
          providers={providers ?? []}
          roles={roles}
          onClose={() => setDialog(null)}
          onSaved={reload}
        />
      )}
      {dialog?.kind === 'named' && (
        <NamedModelDialog
          item={dialog.item}
          providers={providers ?? []}
          toolNames={namedToolNames}
          onClose={() => setDialog(null)}
          onSaved={reload}
        />
      )}
    </section>
  )
}
