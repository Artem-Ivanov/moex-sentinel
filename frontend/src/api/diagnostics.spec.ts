import { afterEach, expect, it, vi } from "vitest"
import { clearSession, currentSession, sessionChecked } from "../auth"
import { router } from "../router"
import { fetchDiagnosticsStatus, type DiagnosticsStatus } from "./diagnostics"

afterEach(() => { clearSession(); sessionChecked.value = false; vi.unstubAllGlobals() })

it("uses the protected endpoint with cancellation", async () => {
  const snapshot = {
    captured_at: "2026-10-03T09:00:00.123Z", status: "UNKNOWN",
    core: { status: "OK", version: "0.1.0", database: "ok", schema: "compatible", reason: "READY" },
    runtime: { environment: "TEST", access_mode: "READ_ONLY" },
    service_versions: {
      worker: { observation: "OBSERVED", version: "1.4.2", received_at: "2026-10-03T09:00:00.000Z", age_ms: 123, reason: "OBSERVED" },
      analytics: { observation: "UNKNOWN", version: null, received_at: null, age_ms: null, reason: "NOT_CONFIGURED" },
    },
    awaiting_observations: ["worker", "broker", "analytics", "market", "outbox", "portfolio", "strategy"],
  } satisfies DiagnosticsStatus
  const fetchMock = vi.fn().mockResolvedValue(Response.json(snapshot))
  vi.stubGlobal("fetch", fetchMock)
  const signal = new AbortController().signal
  expect(await fetchDiagnosticsStatus(signal)).toEqual(snapshot)
  expect(fetchMock).toHaveBeenCalledWith("/api/diagnostics/status", { signal })
})

it.each([403, 500, 503])("rejects HTTP %s without exposing its body", async (status) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("sensitive upstream detail", { status })))
  await expect(fetchDiagnosticsStatus()).rejects.toThrow("Не удалось загрузить диагностику.")
})

it("propagates a network failure", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network")))
  await expect(fetchDiagnosticsStatus()).rejects.toThrow("network")
})

it("applies the existing 401 policy and returns to login with the destination", async () => {
  currentSession.value = { username: "operator", csrf_token: "csrf" }
  sessionChecked.value = true
  await router.push("/diagnostics")
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private", { status: 401 })))
  await expect(fetchDiagnosticsStatus()).rejects.toThrow("Не удалось загрузить диагностику.")
  await vi.waitFor(() => expect(router.currentRoute.value.name).toBe("login"))
  expect(currentSession.value).toBeNull()
  expect(router.currentRoute.value.query.redirect).toBe("/diagnostics")
})

it("protects diagnostics with the existing auth guard", async () => {
  sessionChecked.value = true
  await router.push("/diagnostics")
  expect(router.currentRoute.value.name).toBe("login")
  expect(router.currentRoute.value.query.redirect).toBe("/diagnostics")
})
