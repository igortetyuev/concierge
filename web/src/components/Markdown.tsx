import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

import { mentionRegex } from '../lib/mentions'

interface MdNode {
  type: string
  value?: string
  children?: MdNode[]
  data?: Record<string, unknown>
}

// Containers whose text must stay untouched by mention highlighting.
const SKIP = new Set(['code', 'inlineCode', 'link', 'linkReference', 'definition', 'html'])

function splitMentions(nodes: MdNode[], re: RegExp): MdNode[] {
  const out: MdNode[] = []
  for (const node of nodes) {
    if (node.type === 'text' && typeof node.value === 'string') {
      let last = 0
      for (const m of node.value.matchAll(re)) {
        if (m.index > last) {
          out.push({ type: 'text', value: node.value.slice(last, m.index) })
        }
        out.push({
          type: 'mention',
          children: [{ type: 'text', value: m[0] }],
          data: {
            hName: 'span',
            hProperties: { className: ['md-mention'] },
            hChildren: [{ type: 'text', value: m[0] }],
          },
        })
        last = m.index + m[0].length
      }
      if (last < node.value.length) {
        out.push({ type: 'text', value: node.value.slice(last) })
      }
    } else {
      if (node.children && !SKIP.has(node.type)) {
        node.children = splitMentions(node.children, re)
      }
      out.push(node)
    }
  }
  return out
}

function remarkMentions(re: RegExp | null) {
  return function plugin() {
    return (tree: MdNode) => {
      if (re && Array.isArray(tree.children)) {
        tree.children = splitMentions(tree.children, re)
      }
    }
  }
}

// remark-math understands only $...$/$$...$$ — models often emit \(\)/\[...\].
function normalizeMathDelims(text: string): string {
  const chunks = text.split(/(`{3,}[\s\S]*?(?:`{3,}|$)|`[^`\n]*`)/g)
  return chunks
    .map((chunk) =>
      chunk.startsWith('`')
        ? chunk
        : chunk
            .replace(/\\\[([\s\S]*?)\\\]/g, (_m, inner: string) => `$$${inner}$$`)
            .replace(/\\\(([\s\S]*?)\\\)/g, (_m, inner: string) => `$${inner}$`)
    )
    .join('')
}

export default function Markdown({
  text,
  mentions,
}: {
  text: string
  mentions?: string[]
}) {
  const re = mentionRegex(mentions ?? [])
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkMentions(re)]}
        rehypePlugins={[rehypeKatex]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
        }}
      >
        {normalizeMathDelims(text)}
      </ReactMarkdown>
    </div>
  )
}
