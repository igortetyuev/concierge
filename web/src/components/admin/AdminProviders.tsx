import { useEffect, useState } from 'react'

import { apiClient } from '../../lib/api'
import type { BindingOut, ProviderOut, RoleOut } from '../../lib/api'
import { IconTrash } from '../icons'

const PROVIDER_KINDS = [
  'openai',
  'openai-compatible',
  'anthropic',
  'ollama',
  'google',
  'deepseek',
  'mistral',
  'test',
]

const CAPABILITIES = [
  'chat',
  'chat_fast',
  'vision',
  'stt',
  'tts',
  'embedding',
  'document',
  'code_agent',
]

const EMPTY_PROVIDER = {
  name: '',
  kind: 'openai',
  base_url: '',
  api_key_ref: '',
}

const EMPTY_BINDING = {
  capability: 'chat',
  provider_id: 0,
  model: '',
  role_id: '',
  params: '',
}

export default function AdminProviders() {
  const [providers, setProviders] = useState<ProviderOut[] | null>(null)
  const [bindings, setBindings] = useState<BindingOut[]>([])
  const [roles, setRoles] = useState<RoleOut[]>([])
  const [prov, setProv] = useState(EMPTY_PROVIDER)
  const [bind, setBind] = useState(EMPTY_BINDING)
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [ps, bs, rs] = await Promise.all([
        apiClient.listProviders(),
        apiClient.listBindings(),
        apiClient.listRoles(),
      ])
      setProviders(ps)
      setBindings(bs)
      setRoles(rs)
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

  async function createProvider() {
    if (!prov.name.trim()) return
    try {
      await apiClient.createProvider({
        name: prov.name.trim(),
        kind: prov.kind,
        base_url: prov.base_url.trim() || undefined,
        api_key_ref: prov.api_key_ref.trim() || undefined,
      })
      setProv(EMPTY_PROVIDER)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось создать')
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

  async function createBinding() {
    if (!bind.provider_id || !bind.model.trim()) return
    let params: Record<string, unknown> = {}
    if (bind.params.trim()) {
      try {
        params = JSON.parse(bind.params) as Record<string, unknown>
      } catch {
        setError('params: невалидный JSON')
        return
      }
    }
    try {
      await apiClient.createBinding({
        capability: bind.capability,
        provider_id: bind.provider_id,
        model: bind.model.trim(),
        params,
        role_id: bind.role_id ? Number(bind.role_id) : null,
      })
      setBind(EMPTY_BINDING)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось создать binding')
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

  return (
    <section className="details-section">
      <h3>Провайдеры моделей</h3>
      {providers === null ? (
        <div className="muted">Загрузка…</div>
      ) : (
        providers.map((p) => (
          <div key={p.id} className="memory-item">
            <span className="memory-item-text">
              {p.name} <span className="muted">· {p.kind}</span>
              {p.base_url && <span className="muted"> · {p.base_url}</span>}
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
              title="Удалить провайдера"
              aria-label="Удалить провайдера"
              onClick={() => void removeProvider(p.id)}
            >
              <IconTrash size={16} />
            </button>
          </div>
        ))
      )}
      <div className="settings-form">
        <input
          value={prov.name}
          onChange={(e) => setProv({ ...prov, name: e.target.value })}
          placeholder="имя (напр. winhost-ollama)"
        />
        <select
          value={prov.kind}
          onChange={(e) => setProv({ ...prov, kind: e.target.value })}
        >
          {PROVIDER_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <input
          value={prov.base_url}
          onChange={(e) => setProv({ ...prov, base_url: e.target.value })}
          placeholder="base_url (напр. http://192.168.99.66:11434/v1)"
        />
        <input
          value={prov.api_key_ref}
          onChange={(e) => setProv({ ...prov, api_key_ref: e.target.value })}
          placeholder="api_key_ref (имя настройки/env)"
        />
        <button
          className="btn"
          onClick={() => void createProvider()}
          disabled={!prov.name.trim()}
        >
          Добавить провайдера
        </button>
      </div>

      <h3>Назначение capability → модель</h3>
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
            title="Удалить binding"
            aria-label="Удалить binding"
            onClick={() => void removeBinding(b.id)}
          >
            <IconTrash size={16} />
          </button>
        </div>
      ))}
      <div className="settings-form">
        <select
          value={bind.capability}
          onChange={(e) => setBind({ ...bind, capability: e.target.value })}
        >
          {CAPABILITIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <select
          value={bind.provider_id}
          onChange={(e) =>
            setBind({ ...bind, provider_id: Number(e.target.value) })
          }
        >
          <option value={0}>провайдер…</option>
          {(providers ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <input
          value={bind.model}
          onChange={(e) => setBind({ ...bind, model: e.target.value })}
          placeholder="модель (напр. bge-m3)"
        />
        <select
          value={bind.role_id}
          onChange={(e) => setBind({ ...bind, role_id: e.target.value })}
        >
          <option value="">все роли</option>
          {roles.map((r) => (
            <option key={r.id} value={r.id}>
              только {r.name}
            </option>
          ))}
        </select>
        <input
          value={bind.params}
          onChange={(e) => setBind({ ...bind, params: e.target.value })}
          placeholder='params JSON (напр. {"temperature":0.3})'
        />
        <button
          className="btn"
          onClick={() => void createBinding()}
          disabled={!bind.provider_id || !bind.model.trim()}
        >
          Добавить binding
        </button>
      </div>
      {error && <div className="auth-error">{error}</div>}
    </section>
  )
}
