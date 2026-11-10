import type { MessagePart } from '../lib/api'
import Markdown from './Markdown'
import { IconCheck, IconClose, IconTool } from './icons'

export function ToolChip({ name, status }: { name: string; status?: string }) {
  return (
    <span className={`tool-chip status-${status ?? 'running'}`}>
      <IconTool size={13} />
      <span className="tool-name">{name}</span>
      {status === 'done' && <IconCheck size={13} />}
      {status === 'error' && <IconClose size={13} />}
      {(status === 'running' || !status) && <span className="spinner" />}
    </span>
  )
}

export default function MessageParts({ parts }: { parts: MessagePart[] }) {
  return (
    <>
      {parts.map((p, i) => {
        if (p.type === 'text') return <Markdown key={i} text={String(p.text ?? '')} />
        if (p.type === 'tool') {
          return <ToolChip key={p.tool_call_id ?? i} name={String(p.name ?? 'tool')} status={p.status} />
        }
        return (
          <span key={i} className="part-attachment">
            [{p.type}]
          </span>
        )
      })}
    </>
  )
}
