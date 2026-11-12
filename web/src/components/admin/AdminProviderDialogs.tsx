import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'

import { apiClient } from '../../lib/api'
import type {
  BindingOut,
  NamedModelOut,
  ProviderOut,
  RoleOut,
} from '../../lib/api'
import { IconClose } from '../icons'

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

interface DialogProps {
  onClose: () => void
  onSaved: () => Promise<void>
}

function Modal({
  title,
  error,
  busy,
  disabled,
  onClose,
  onSubmit,
  children,
}: {
  title: string
  error: string
  busy: boolean
  disabled: boolean
  onClose: () => void
  onSubmit: (e: FormEvent) => void
  children: ReactNode
}) {
  return (
    <div className="modal-scrim" onClick={onClose}>
      <form
        className="modal"
        onClick={(e) => e.stopPropagation()}
        onSubmit={onSubmit}
        aria-label={title}
      >
        <div className="modal-head">
          <h2>{title}</h2>
          <button
            type="button"
            className="icon-btn"
            onClick={onClose}
            aria-label="Закрыть"
          >
            <IconClose />
          </button>
        </div>
        {children}
        {error && (
          <div className="auth-error" role="alert">
            {error}
          </div>
        )}
        <button
          className="btn primary"
          type="submit"
          disabled={busy || disabled}
        >
          {busy ? '…' : 'Сохранить'}
        </button>
      </form>
    </div>
  )
}

function errText(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback
}

export function ProviderDialog({
  item,
  onClose,
  onSaved,
}: DialogProps & { item: ProviderOut | null }) {
  const [name, setName] = useState(item?.name ?? '')
  const [kind, setKind] = useState(item?.kind ?? 'openai-compatible')
  const [baseUrl, setBaseUrl] = useState(item?.base_url ?? '')
  const [keyRef, setKeyRef] = useState(item?.api_key_ref ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (item) {
        await apiClient.patchProvider(item.id, {
          name: name.trim(),
          kind,
          base_url: baseUrl.trim() || null,
          api_key_ref: keyRef.trim() || null,
        })
      } else {
        await apiClient.createProvider({
          name: name.trim(),
          kind,
          base_url: baseUrl.trim() || undefined,
          api_key_ref: keyRef.trim() || undefined,
        })
      }
      await onSaved()
      onClose()
    } catch (e) {
      setError(errText(e, 'Не удалось сохранить'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={item ? 'Редактировать провайдера' : 'Новый провайдер'}
      error={error}
      busy={busy}
      disabled={!name.trim()}
      onClose={onClose}
      onSubmit={(e) => void submit(e)}
    >
      <label className="field">
        <span>Имя</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="напр. yandex, local-ollama"
          autoFocus
        />
      </label>
      <label className="field">
        <span>Тип</span>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {PROVIDER_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Base URL</span>
        <input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="напр. https://ai.api.cloud.yandex.net/v1"
        />
      </label>
      <label className="field">
        <span>API key ref</span>
        <input
          value={keyRef}
          onChange={(e) => setKeyRef(e.target.value)}
          placeholder="имя настройки/env — или сам ключ"
        />
      </label>
    </Modal>
  )
}

export function BindingDialog({
  item,
  providers,
  roles,
  onClose,
  onSaved,
}: DialogProps & {
  item: BindingOut | null
  providers: ProviderOut[]
  roles: RoleOut[]
}) {
  const [capability, setCapability] = useState(item?.capability ?? 'chat')
  const [providerId, setProviderId] = useState(item?.provider_id ?? 0)
  const [model, setModel] = useState(item?.model ?? '')
  const [roleId, setRoleId] = useState(
    item?.role_id != null ? String(item.role_id) : '',
  )
  const [params, setParams] = useState(
    item && Object.keys(item.params).length ? JSON.stringify(item.params) : '',
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    let parsed: Record<string, unknown> = {}
    if (params.trim()) {
      try {
        parsed = JSON.parse(params) as Record<string, unknown>
      } catch {
        setError('params: невалидный JSON')
        return
      }
    }
    setBusy(true)
    setError('')
    try {
      const body = {
        capability,
        provider_id: providerId,
        model: model.trim(),
        params: parsed,
        role_id: roleId ? Number(roleId) : null,
      }
      if (item) await apiClient.patchBinding(item.id, body)
      else await apiClient.createBinding(body)
      await onSaved()
      onClose()
    } catch (e) {
      setError(errText(e, 'Не удалось сохранить'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={item ? 'Редактировать binding' : 'Новый binding'}
      error={error}
      busy={busy}
      disabled={!providerId || !model.trim()}
      onClose={onClose}
      onSubmit={(e) => void submit(e)}
    >
      <label className="field">
        <span>Capability</span>
        <select
          value={capability}
          onChange={(e) => setCapability(e.target.value)}
        >
          {CAPABILITIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Провайдер</span>
        <select
          value={providerId}
          onChange={(e) => setProviderId(Number(e.target.value))}
        >
          <option value={0}>выбрать…</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Модель</span>
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="напр. bge-m3, gpt://b1g…/aliceai-llm/latest"
        />
      </label>
      <label className="field">
        <span>Роль</span>
        <select value={roleId} onChange={(e) => setRoleId(e.target.value)}>
          <option value="">все роли</option>
          {roles.map((r) => (
            <option key={r.id} value={r.id}>
              только {r.name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Params (JSON)</span>
        <input
          value={params}
          onChange={(e) => setParams(e.target.value)}
          placeholder='{"temperature":0.3}'
        />
      </label>
    </Modal>
  )
}

export function NamedModelDialog({
  item,
  providers,
  toolNames,
  onClose,
  onSaved,
}: DialogProps & {
  item: NamedModelOut | null
  providers: ProviderOut[]
  toolNames: string[]
}) {
  const [name, setName] = useState(item?.name ?? '')
  const [providerId, setProviderId] = useState(item?.provider_id ?? 0)
  const [model, setModel] = useState(item?.model ?? '')
  const [systemPrompt, setSystemPrompt] = useState(item?.system_prompt ?? '')
  const [tools, setTools] = useState<string[]>(item?.tools ?? [])
  const [params, setParams] = useState(
    item && Object.keys(item.params).length ? JSON.stringify(item.params) : '',
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  function toggleTool(t: string) {
    setTools((prev) =>
      prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t],
    )
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    let parsed: Record<string, unknown> = {}
    if (params.trim()) {
      try {
        parsed = JSON.parse(params) as Record<string, unknown>
      } catch {
        setError('params: невалидный JSON')
        return
      }
    }
    setBusy(true)
    setError('')
    try {
      if (item) {
        await apiClient.patchNamedModel(item.id, {
          name: name.trim(),
          provider_id: providerId,
          model: model.trim(),
          params: parsed,
          system_prompt: systemPrompt.trim() || null,
          tools,
        })
      } else {
        await apiClient.createNamedModel({
          name: name.trim(),
          provider_id: providerId,
          model: model.trim(),
          params: parsed,
          system_prompt: systemPrompt.trim() || undefined,
          tools,
        })
      }
      await onSaved()
      onClose()
    } catch (e) {
      setError(errText(e, 'Не удалось сохранить'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={item ? 'Редактировать модель' : 'Новая именованная модель'}
      error={error}
      busy={busy}
      disabled={!name.trim() || !providerId || !model.trim()}
      onClose={onClose}
      onSubmit={(e) => void submit(e)}
    >
      <label className="field">
        <span>Имя</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="напр. Алиса"
          autoFocus
        />
      </label>
      <label className="field">
        <span>Провайдер</span>
        <select
          value={providerId}
          onChange={(e) => setProviderId(Number(e.target.value))}
        >
          <option value={0}>выбрать…</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Модель</span>
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="напр. gpt://b1g…/aliceai-llm/latest"
        />
      </label>
      <label className="field">
        <span>Системный промпт (необязательно)</span>
        <input
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          placeholder="напр. Отвечай как Алиса."
        />
      </label>
      <div className="field">
        <span>Инструменты</span>
        {toolNames.map((t) => (
          <label key={t} className="tool-check">
            <input
              type="checkbox"
              checked={tools.includes(t)}
              onChange={() => toggleTool(t)}
            />
            {t}
          </label>
        ))}
      </div>
      <label className="field">
        <span>Params (JSON)</span>
        <input
          value={params}
          onChange={(e) => setParams(e.target.value)}
          placeholder='{"temperature":0.3}'
        />
      </label>
    </Modal>
  )
}
