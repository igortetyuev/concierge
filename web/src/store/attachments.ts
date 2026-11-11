import { atom } from 'nanostores'

import type { AttachmentOut } from '../lib/api'

/** Latest known pipeline state per attachment id (WS `attachment.updated`). */
export const $attachments = atom<Record<number, AttachmentOut>>({})

export function setAttachment(att: AttachmentOut): void {
  $attachments.set({ ...$attachments.get(), [att.id]: att })
}
