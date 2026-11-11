import { atom } from 'nanostores'

export const $detailsOpen = atom(false)

/** Set to drop text into the conversation composer (transcript correction). */
export const $composerPrefill = atom<{ convId: number; text: string } | null>(null)
