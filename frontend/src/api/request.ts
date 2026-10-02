import { currentSession, handleUnauthorized } from "../auth"

const mutationMethods = new Set(["POST", "PUT", "PATCH", "DELETE"])

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const hasInit = arguments.length > 1
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase()
  const protectedApi = typeof input === "string" && input.startsWith("/api/") &&
    !input.startsWith("/api/auth/") && input !== "/api/health"

  let response: Response
  if (currentSession.value && protectedApi && mutationMethods.has(method)) {
    const headers = new Headers(init?.headers)
    headers.set("X-CSRF-Token", currentSession.value.csrf_token)
    response = await fetch(input, { ...init, headers })
  } else {
    response = hasInit ? await fetch(input, init) : await fetch(input)
  }

  if (response.status === 401 && protectedApi) handleUnauthorized()
  return response
}
