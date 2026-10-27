import { afterEach, expect, it, vi } from "vitest"
import { createMemoryHistory, createRouter } from "vue-router"

import { AuthError, clearSession, currentSession, login, logout, restoreSession, safeInternalRedirect, sessionChecked } from "./auth"
import { apiFetch } from "./api/request"
import { installAuthGuard } from "./router"

afterEach(() => {
  clearSession()
  sessionChecked.value = false
  vi.unstubAllGlobals()
})

it("restores a server session after a page reload", async () => {
  const fetchMock = vi.fn().mockResolvedValue(Response.json({ username: "operator", csrf_token: "csrf" }))
  vi.stubGlobal("fetch", fetchMock)
  await restoreSession()
  expect(currentSession.value?.username).toBe("operator")
  expect(sessionChecked.value).toBe(true)
  expect(fetchMock).toHaveBeenCalledWith("/api/auth/session")
})

it("adds CSRF only to a signed-in mutation", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ username: "operator", csrf_token: "csrf" })))
  await login("operator", "a long test password")
  const fetchMock = vi.fn().mockResolvedValue(Response.json({ ok: true }))
  vi.stubGlobal("fetch", fetchMock)
  await apiFetch("/api/brokers", { method: "POST", headers: { "Content-Type": "application/json" } })
  expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("X-CSRF-Token")).toBe("csrf")
  await apiFetch("/api/brokers")
  expect(fetchMock).toHaveBeenLastCalledWith("/api/brokers")
})

it("revokes a session on logout", async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({ username: "operator", csrf_token: "csrf" }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
  vi.stubGlobal("fetch", fetchMock)
  await login("operator", "a long test password")
  await logout()
  expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("X-CSRF-Token")).toBe("csrf")
  expect(currentSession.value).toBeNull()
})

it("keeps the session when logout fails", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ username: "operator", csrf_token: "csrf" }))
    .mockResolvedValueOnce(new Response(null, { status: 503 }))
  vi.stubGlobal("fetch", fetchMock)
  await login("operator", "a long test password")

  await expect(logout()).rejects.toBeInstanceOf(AuthError)
  expect(currentSession.value?.username).toBe("operator")
})

it("keeps the session when the logout request has a network error", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ username: "operator", csrf_token: "csrf" }))
    .mockRejectedValueOnce(new TypeError("network error"))
  vi.stubGlobal("fetch", fetchMock)
  await login("operator", "a long test password")

  await expect(logout()).rejects.toThrow("network error")
  expect(currentSession.value?.username).toBe("operator")
})

it("accepts only internal redirects without backslashes or control characters", () => {
  expect(safeInternalRedirect("/positions/42")).toBe("/positions/42")
  expect(safeInternalRedirect("//outside.example/path")).toBeNull()
  expect(safeInternalRedirect("/\\\\outside.example/path")).toBeNull()
  expect(safeInternalRedirect("/positions/\u0007")).toBeNull()
})

it("redirects protected routes to login and keeps the internal destination", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", name: "login", component: { template: "<div>login</div>" } },
      { path: "/positions/:id", name: "position", component: { template: "<div>position</div>" } },
    ],
  })
  installAuthGuard(router)
  await router.push("/positions/42")
  expect(router.currentRoute.value.name).toBe("login")
  expect(router.currentRoute.value.query.redirect).toBe("/positions/42")
})

it("clears the session after a protected API returns 401", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json({ username: "operator", csrf_token: "csrf" }))
    .mockResolvedValueOnce(new Response(null, { status: 401 })))
  await login("operator", "a long test password")
  await apiFetch("/api/brokers")
  expect(currentSession.value).toBeNull()
})
