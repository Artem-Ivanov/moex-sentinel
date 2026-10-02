import { ref } from "vue"

export interface AuthSession {
  username: string
  csrf_token: string
}

export const currentSession = ref<AuthSession | null>(null)
export const sessionChecked = ref(false)

let restoreRequest: Promise<void> | undefined
let unauthorizedHandler: (() => void) | undefined

export class AuthError extends Error {
  constructor(readonly status: number) {
    super("Authentication request failed")
  }
}

export function clearSession(): void {
  currentSession.value = null
}

export function safeInternalRedirect(value: unknown): string | null {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(value)) {
    return null
  }
  try {
    return new URL(value, window.location.origin).origin === window.location.origin ? value : null
  } catch {
    return null
  }
}

export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler
}

export function handleUnauthorized(): void {
  clearSession()
  sessionChecked.value = true
  unauthorizedHandler?.()
}

export async function restoreSession(): Promise<void> {
  if (restoreRequest) return restoreRequest
  restoreRequest = (async () => {
    try {
      const response = await fetch("/api/auth/session")
      if (!response.ok) {
        clearSession()
        return
      }
      currentSession.value = (await response.json()) as AuthSession
    } catch {
      clearSession()
    } finally {
      sessionChecked.value = true
      restoreRequest = undefined
    }
  })()
  return restoreRequest
}

export async function login(username: string, password: string): Promise<void> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  })
  if (!response.ok) throw new AuthError(response.status)
  currentSession.value = (await response.json()) as AuthSession
  sessionChecked.value = true
}

export async function logout(): Promise<void> {
  const csrfToken = currentSession.value?.csrf_token
  const response = await fetch("/api/auth/logout", {
    method: "POST",
    headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
  })
  if (response.status === 401) {
    clearSession()
    sessionChecked.value = true
    return
  }
  if (!response.ok) throw new AuthError(response.status)
  clearSession()
  sessionChecked.value = true
}
