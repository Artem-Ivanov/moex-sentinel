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
    worker: {
      observation: "OBSERVED", status: "DEGRADED", reason: "OUTBOX_BLOCKED", instance_id: "123e4567-e89b-12d3-a456-426614174000",
      received_at: "2026-10-03T09:00:00.000Z", age_ms: 123, completed_iterations: 4,
      last_completed_at: "2026-10-03T08:58:00.000Z", completion_age_ms: 120123,
      last_finished_at: "2026-10-03T08:59:00.000Z", last_result: "OUTBOX_BLOCKED", error_code: null,
      outbox: {
        observation: "OBSERVED", status: "DEGRADED", reason: "OLD_PENDING", pending_count: 2, failed_count: 1,
        oldest_pending_at: "2026-10-03T08:30:00.000Z", oldest_pending_age_ms: 1800123, max_retry_count: 5,
      },
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
