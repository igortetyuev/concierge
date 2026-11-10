import { atom } from 'nanostores'

import { apiClient, onAuthLost, refreshToken, setAccessToken } from '../lib/api'
import type { UserOut } from '../lib/api'

export type AuthStatus = 'loading' | 'anon' | 'authed'

export const $authStatus = atom<AuthStatus>('loading')
export const $user = atom<UserOut | null>(null)

onAuthLost(() => {
  $user.set(null)
  $authStatus.set('anon')
})

/** Boot: turn the httpOnly refresh cookie into an access token. */
export async function initAuth(): Promise<void> {
  try {
    const t = await refreshToken()
    $user.set(t.user)
    $authStatus.set('authed')
  } catch {
    $authStatus.set('anon')
  }
}

export async function login(username: string, password: string): Promise<void> {
  const t = await apiClient.login(username, password)
  setAccessToken(t.access_token, t.expires_in)
  $user.set(t.user)
  $authStatus.set('authed')
}

export async function register(
  token: string,
  username: string,
  displayName: string,
  password: string,
): Promise<void> {
  const t = await apiClient.register(token, username, displayName, password)
  setAccessToken(t.access_token, t.expires_in)
  $user.set(t.user)
  $authStatus.set('authed')
}

export async function logout(): Promise<void> {
  try {
    await apiClient.logout()
  } finally {
    $user.set(null)
    $authStatus.set('anon')
  }
}
