import { afterEach, expect, it, vi } from "vitest"
import { currentSession } from "../auth"
import { fetchTradingAdmission, fetchTradingPreparation, updateTradingAdmissionLimits } from "./tradingAdmission"

const brokerId = "11111111-1111-4111-8111-111111111111"
const accountId = "account-1"
const runtimeId = "22222222-2222-4222-8222-222222222222"
const baselineId = "33333333-3333-4333-8333-333333333333"
const reconciliationId = "44444444-4444-4444-8444-444444444444"
const fingerprint = "a".repeat(64)

const limits = {
  currency: "RUB", max_order_notional: "1000", exposure_scope: "ACCOUNT_GROSS",
  max_exposure: "5000", max_outstanding_orders: 2, max_daily_loss: "5000",
}

const admission = {
  broker_id: brokerId,
  account_id: accountId,
  revision: 3,
  owner_runtime_instance_id: runtimeId,
  owner_generation: 2,
  recovery_runtime_instance_id: null,
  admission_status: "DISARMED",
  arm_revision: null,
  lease_expires_at: "2026-10-14T10:00:30.000Z",
  arm_expires_at: null,
  limits: null,
  configuration_fingerprint: null,
  baseline: {
    baseline_id: baselineId, broker_id: brokerId, account_id: accountId, moscow_day: "2026-10-14",
  },
  reconciliation: {
    reconciliation_id: reconciliationId, broker_id: brokerId, account_id: accountId, scope: "CANARY_UID",
    owner_runtime_instance_id: runtimeId, owner_generation: 2, configuration_fingerprint: fingerprint,
    status: "CANARY_READY", canary_uid: "uid-1", captured_at: "2026-10-14T09:59:30.000Z",
    valid_until: "2026-10-14T10:00:30.000Z", can_arm: true, reasons: [], active_ordinary_order_count: 0,
    active_stop_order_count: 0, unresolved_reservation_count: 0, position_anchor_units: null, risk_snapshot_id: null,
  },
  effective_can_submit: false,
  effective_denial_reasons: ["DISARMED"],
  unresolved_reservation_count: 0,
  outstanding_order_count: 0,
  recovery_only: false,
}

const waiting = {
  moscow_day: null, captured_at: null, status: "WAITING", reason: "NOT_OBSERVED", baseline_id: null,
  owner_runtime_instance_id: null, owner_generation: null, configuration_fingerprint: null,
  reconciliation_id: null, reconciliation_valid_until: null, suggested_limits: null, risk_reasons: [],
}

afterEach(() => { vi.unstubAllGlobals(); currentSession.value = null })

it("loads validated admission and daily preparation with read-only GETs", async () => {
  const preparation = { ...waiting, moscow_day: "2026-10-14", captured_at: "2026-10-14T10:00:00.000Z" }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(admission))
    .mockResolvedValueOnce(Response.json(preparation))
  vi.stubGlobal("fetch", fetchMock)
  const controller = new AbortController()

  await expect(fetchTradingAdmission(brokerId)).resolves.toEqual(admission)
  await expect(fetchTradingPreparation(brokerId, controller.signal)).resolves.toEqual(preparation)

  expect(fetchMock).toHaveBeenNthCalledWith(1, `/api/brokers/${brokerId}/trading-admission`, undefined)
  expect(fetchMock).toHaveBeenNthCalledWith(2, `/api/brokers/${brokerId}/trading-admission/preparation`, { signal: controller.signal })
  expect(fetchMock.mock.calls.every(([, init]) => init?.method === undefined || init.method === "GET")).toBe(true)
})

it("accepts the Core's empty DISABLED and NOT_OBSERVED preparation views", async () => {
  const disabled = { ...waiting, status: "DISABLED", reason: "DAILY_PREPARATION_DISABLED" }
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(disabled))
    .mockResolvedValueOnce(Response.json(waiting)))

  await expect(fetchTradingPreparation(brokerId)).resolves.toEqual(disabled)
  await expect(fetchTradingPreparation(brokerId)).resolves.toEqual(waiting)
})

it("accepts BLOCKED/SCOPE_UNAVAILABLE without an observation timestamp", async () => {
  const unavailableScope = { ...waiting, status: "BLOCKED", reason: "SCOPE_UNAVAILABLE" }
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(unavailableScope)))
  await expect(fetchTradingPreparation(brokerId)).resolves.toEqual(unavailableScope)
})

it.each([
  ["truthy non-boolean submit flag", { ...admission, effective_can_submit: "false" }],
  ["unknown admission status", { ...admission, admission_status: "READY" }],
  ["missing owner generation", { ...admission, owner_generation: undefined }],
  ["invalid owner UUID", { ...admission, owner_runtime_instance_id: "owner" }],
  ["expired date encoding", { ...admission, lease_expires_at: "yesterday" }],
  ["invalid limits string", { ...admission, limits: { ...limits, max_exposure: "Infinity" } }],
  ["NaN limit string", { ...admission, limits: { ...limits, max_exposure: "NaN" } }],
  ["overflowing exponent", { ...admission, limits: { ...limits, max_exposure: "1E+9999" } }],
  ["non-string reasons", { ...admission, effective_denial_reasons: [{ reason: "DISARMED" }] }],
])("rejects malformed account view: %s", async (_name, malformed) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(malformed)))
  await expect(fetchTradingAdmission(brokerId)).rejects.toThrow("Не удалось проверить допуск счёта.")
})

it("accepts unknown future denial reason text as a safe string array", async () => {
  const futureReasonView = { ...admission, effective_denial_reasons: ["FUTURE_REASON"] }
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(futureReasonView)))
  await expect(fetchTradingAdmission(brokerId)).resolves.toEqual(futureReasonView)
})

it("accepts finite scientific-notation decimal strings from Core limits", async () => {
  const scientificLimits = { ...limits, max_order_notional: "1E-7", max_exposure: "1E+7" }
  const prepared = {
    ...waiting,
    moscow_day: "2026-10-14",
    captured_at: "2026-10-14T10:00:00.000Z",
    status: "PREPARED",
    reason: "ARM_REQUIRED",
    baseline_id: baselineId,
    owner_runtime_instance_id: runtimeId,
    owner_generation: 2,
    configuration_fingerprint: fingerprint,
    reconciliation_id: reconciliationId,
    reconciliation_valid_until: "2026-10-14T10:00:30.000Z",
    suggested_limits: scientificLimits,
  }
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json({ ...admission, limits: scientificLimits }))
    .mockResolvedValueOnce(Response.json(prepared)))

  await expect(fetchTradingAdmission(brokerId)).resolves.toMatchObject({ limits: scientificLimits })
  await expect(fetchTradingPreparation(brokerId)).resolves.toMatchObject({ suggested_limits: scientificLimits })
})

it("treats an HTTP 200 null body as malformed, not as an absent admission", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(null)))
  await expect(fetchTradingAdmission(brokerId)).rejects.toThrow("Не удалось проверить допуск счёта.")
})

it.each([
  ["prepared without identity", { ...waiting, status: "PREPARED", reason: "ARM_REQUIRED" }],
  ["unknown prep reason", { ...waiting, reason: "UNKNOWN" }],
  ["future capture timestamp", { ...waiting, moscow_day: "2026-10-14", captured_at: "not-a-date" }],
])("rejects malformed preparation view: %s", async (_name, malformed) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(malformed)))
  await expect(fetchTradingPreparation(brokerId)).rejects.toThrow("Не удалось проверить подготовку допуска.")
})

it("treats an absent read-only admission or preparation view as unavailable", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(new Response(null, { status: 404 }))
    .mockResolvedValueOnce(new Response(null, { status: 404 })))

  await expect(fetchTradingAdmission(brokerId)).resolves.toBeNull()
  await expect(fetchTradingPreparation(brokerId)).resolves.toBeNull()
})

it("updates account-wide limits with the current operator CSRF token and validates the returned view", async () => {
  const accountLimits = {
    currency: "RUB", exposure_scope: "ACCOUNT_GROSS", max_order_notional: "1250",
    max_exposure: "5000", max_outstanding_orders: 2, max_daily_loss: "250",
  } as const
  const updated = { ...admission, revision: 4, limits: accountLimits }
  currentSession.value = { username: "operator", csrf_token: "csrf-token" }
  const fetchMock = vi.fn().mockResolvedValue(Response.json(updated))
  vi.stubGlobal("fetch", fetchMock)

  await expect(updateTradingAdmissionLimits(brokerId, accountId, 3, accountLimits)).resolves.toEqual(updated)

  const [url, init] = fetchMock.mock.calls[0]
  expect(url).toBe(`/api/brokers/${brokerId}/trading-admission/limits`)
  expect(init.method).toBe("PUT")
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf-token")
  expect(init.body).toBe(JSON.stringify({ expected_revision: 3, limits: accountLimits }))
})

it("does not accept an update response for a different account scope", async () => {
  const accountLimits = {
    currency: "RUB", exposure_scope: "ACCOUNT_GROSS", max_order_notional: "1250",
    max_exposure: "5000", max_outstanding_orders: 2, max_daily_loss: "250",
  } as const
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...admission, account_id: "another-account" })))

  await expect(updateTradingAdmissionLimits(brokerId, accountId, 3, accountLimits)).rejects.toThrow()
})

it.each(["0.00000001", "1e-8"])("compares %s exactly with Core scientific notation", async (inputNotional) => {
  const accountLimits = {
    currency: "RUB", exposure_scope: "ACCOUNT_GROSS", max_order_notional: inputNotional,
    max_exposure: "5000", max_outstanding_orders: 2, max_daily_loss: "250",
  } as const
  const normalized = { ...admission, revision: 4, limits: { ...accountLimits, max_order_notional: "1E-8" } }
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(normalized)))

  await expect(updateTradingAdmissionLimits(brokerId, accountId, 3, accountLimits)).resolves.toEqual(normalized)
})

it("does not turn Core errors into an empty admission view", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private backend detail", { status: 503 })))

  await expect(fetchTradingAdmission(brokerId)).rejects.toThrow("Не удалось проверить допуск счёта.")
  await expect(fetchTradingPreparation(brokerId)).rejects.toThrow("Не удалось проверить подготовку допуска.")
})
