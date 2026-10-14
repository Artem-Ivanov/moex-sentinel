import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import TradingAdmissionStatus from "./TradingAdmissionStatus.vue"

const moscowDay = "2026-10-14"
const brokerId = "11111111-1111-4111-8111-111111111111"
const brokerA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
const brokerB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
const runtimeId = "22222222-2222-4222-8222-222222222222"
const baselineId = "33333333-3333-4333-8333-333333333333"
const reconciliationId = "44444444-4444-4444-8444-444444444444"
const fingerprint = "a".repeat(64)
const limits = {
  canary_uid: "uid-1", currency: "RUB", max_order_notional: "1000", exposure_scope: "CANARY_UID_GROSS",
  max_exposure: "5000", max_outstanding_orders: 2, max_daily_loss: "5000",
}

const admission = {
  broker_id: brokerId,
  account_id: "account-1",
  revision: 3,
  owner_runtime_instance_id: runtimeId,
  owner_generation: 2,
  recovery_runtime_instance_id: runtimeId,
  admission_status: "STOP_REQUIRED",
  arm_revision: null,
  lease_expires_at: "2026-10-14T10:00:30.000Z",
  arm_expires_at: null,
  limits: null,
  configuration_fingerprint: null,
  baseline: { baseline_id: baselineId, broker_id: brokerId, account_id: "account-1", moscow_day: moscowDay },
  reconciliation: {
    reconciliation_id: reconciliationId,
    broker_id: brokerId,
    account_id: "account-1",
    scope: "CANARY_UID",
    owner_runtime_instance_id: runtimeId,
    owner_generation: 2,
    configuration_fingerprint: fingerprint,
    status: "DATA_UNKNOWN",
    canary_uid: "uid-1",
    captured_at: "2026-10-14T09:59:30.000Z",
    valid_until: "2026-10-14T10:00:00.000Z",
    can_arm: false,
    reasons: ["STALE_MARKET"],
    active_ordinary_order_count: 0,
    active_stop_order_count: 0,
    unresolved_reservation_count: 0,
    position_anchor_units: null,
    risk_snapshot_id: null,
  },
  effective_can_submit: false,
  effective_denial_reasons: ["STOP_REQUIRED", "DISARMED", "FUTURE_REASON"],
  unresolved_reservation_count: 0,
  outstanding_order_count: 0,
  recovery_only: true,
}

const preparation = {
  moscow_day: moscowDay,
  captured_at: "2026-10-14T09:59:45.000Z",
  status: "BLOCKED",
  reason: "RECOVERY_REQUIRED",
  baseline_id: baselineId,
  owner_runtime_instance_id: runtimeId,
  owner_generation: 2,
  configuration_fingerprint: fingerprint,
  reconciliation_id: null,
  reconciliation_valid_until: null,
  suggested_limits: null,
  risk_reasons: ["STALE_MARKET"],
}

function admissionResponse(value: unknown = admission) { return Response.json(value) }
function preparationResponse(value: unknown = preparation) { return Response.json(value) }
function armableAdmissionView() {
  return {
    ...admission,
    admission_status: "DISARMED",
    recovery_runtime_instance_id: null,
    recovery_only: false,
    effective_can_submit: false,
    effective_denial_reasons: ["DISARMED"],
    reconciliation: { ...admission.reconciliation, status: "CANARY_READY", valid_until: "2026-10-14T10:00:30.000Z", can_arm: true, reasons: [] },
  }
}
function armedAdmissionView() {
  return {
    ...admission,
    admission_status: "ARMED",
    arm_revision: 4,
    recovery_runtime_instance_id: null,
    arm_expires_at: "2026-10-14T10:00:30.000Z",
    limits,
    recovery_only: false,
    effective_can_submit: true,
    effective_denial_reasons: [] as string[],
    reconciliation: { ...admission.reconciliation, status: "CANARY_READY", valid_until: "2026-10-14T10:00:30.000Z", can_arm: true, reasons: [] },
  }
}
function preparedView(overrides: Record<string, unknown> = {}) {
  return {
    ...preparation,
    status: "PREPARED",
    reason: "ARM_REQUIRED",
    baseline_id: baselineId,
    owner_runtime_instance_id: runtimeId,
    owner_generation: 2,
    configuration_fingerprint: fingerprint,
    reconciliation_id: reconciliationId,
    reconciliation_valid_until: "2026-10-14T10:00:30.000Z",
    risk_reasons: [],
    suggested_limits: {
      canary_uid: "uid-1", currency: "RUB", max_order_notional: "1000", exposure_scope: "CANARY_UID_GROSS",
      max_exposure: "5000", max_outstanding_orders: 2, max_daily_loss: "5000",
    },
    ...overrides,
  }
}
function props(selectedBrokerId = brokerId, accountId = "account-1") {
  return { brokerId: selectedBrokerId, accountId, positionState: "HOLD", resumeRequested: true }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it("shows position HOLD/resume state separately from account and risk blockers", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse())
    .mockResolvedValueOnce(preparationResponse()))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText("Статус Core:")).toBeTruthy()
  expect(screen.getByText("HOLD")).toBeTruthy()
  expect(screen.getByText("Запрос Resume принят.")).toBeTruthy()
  expect(screen.getByText("STOP_REQUIRED")).toBeTruthy()
  expect(screen.getByText("Управление остановкой требуется; продолжите восстановление через оператора.")).toBeTruthy()
  expect(screen.getByText("Нужна явная сверка/восстановление допуска оператором; остановка сохраняется.")).toBeTruthy()
  expect(screen.getByText("Неизвестная причина блокировки. Считайте статус неизвестным и передайте оператору.")).toBeTruthy()
  expect(screen.getByText("Риск/рынок: цена устарела.")).toBeTruthy()
  expect(screen.getByText("Режим TRADE и эта сводка не подтверждают свежую цену или доступность брокера для заявки.")).toBeTruthy()
})

it.each([
  ["DISABLED", "DAILY_PREPARATION_DISABLED"],
  ["WAITING", "NOT_OBSERVED"],
  ["BLOCKED", "SCOPE_UNAVAILABLE"],
])("renders %s without a day as its safe reason, not a day-change warning", async (status, reason) => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse())
    .mockResolvedValueOnce(preparationResponse({ ...preparation, moscow_day: null, captured_at: null, status, reason })))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText(new RegExp(`${reason}:`))).toBeTruthy()
  expect(screen.queryByText("Подготовка относится к другому московскому дню; требуется новая проверка.")).toBeNull()
})

it("shows PREPARED only as readiness for explicit ARM, with preview limits", async () => {
  const prepared = preparedView()
  const armableAdmission = armableAdmissionView()
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse(armableAdmission))
    .mockResolvedValueOnce(preparationResponse(prepared)))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText("Готово только к явному ARM оператора.")).toBeTruthy()
  expect(screen.queryByText("Допуск счёта подтверждён последним снимком Core.")).toBeNull()
  expect(screen.getByText("1000 RUB")).toBeTruthy()
  expect(screen.getByText("Это preview лимитов; ARM и возобновление позиции не выполняются интерфейсом.")).toBeTruthy()
})

it("removes PREPARED readiness when the cached preparation view reaches its freshness TTL", async () => {
  vi.useRealTimers()
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  const prepared = preparedView()
  const slowRefresh = new Promise<Response>(() => undefined)
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse(armableAdmissionView()))
    .mockResolvedValueOnce(preparationResponse(prepared))
    .mockReturnValue(slowRefresh))
  render(TradingAdmissionStatus, { props: props() })
  expect(await screen.findByText("Готово только к явному ARM оператора.")).toBeTruthy()

  await vi.advanceTimersByTimeAsync(15_000)
  expect(screen.getByText("Снимок подготовки устарел; требуется повторная проверка перед ARM.")).toBeTruthy()
  expect(screen.queryByText("Готово только к явному ARM оператора.")).toBeNull()
})

it("keeps account admission visible when daily preparation endpoint is absent", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse())
    .mockResolvedValueOnce(new Response(null, { status: 404 })))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText("STOP_REQUIRED")).toBeTruthy()
  expect(screen.getByText("Ежедневная подготовка не опубликована или отключена.")).toBeTruthy()
})

it("clears a previously green admission after Core becomes unavailable", async () => {
  const ready = { ...armedAdmissionView(), arm_expires_at: "2026-10-14T10:01:00.000Z" }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(admissionResponse(ready))
    .mockResolvedValueOnce(preparationResponse())
    .mockResolvedValueOnce(new Response("offline", { status: 503 }))
    .mockResolvedValueOnce(new Response(null, { status: 404 }))
  vi.stubGlobal("fetch", fetchMock)
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText("Допуск счёта подтверждён последним снимком Core.")).toBeTruthy()
  await fireEvent.click(screen.getByRole("button", { name: "Обновить допуск" }))
  expect(await screen.findByText("Статус допуска неизвестен: Core недоступен.")).toBeTruthy()
  expect(screen.queryByText("Допуск счёта подтверждён последним снимком Core.")).toBeNull()
})

it("does not show account access when Core fields contradict the armed status", async () => {
  const contradictory = {
    ...admission,
    admission_status: "DISARMED",
    recovery_only: false,
    effective_can_submit: true,
    effective_denial_reasons: [],
    arm_expires_at: "2026-10-14T10:00:30.000Z",
  }
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse(contradictory))
    .mockResolvedValueOnce(preparationResponse()))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText("DISARMED")).toBeTruthy()
  expect(screen.queryByText("Допуск счёта подтверждён последним снимком Core.")).toBeNull()
  expect(screen.getByText("Допуск не подтверждён; требуется проверка оператора.")).toBeTruthy()
})

it("does not show account access for an ARMED view without validated limits", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse({ ...armedAdmissionView(), limits: null }))
    .mockResolvedValueOnce(preparationResponse()))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText("ARMED")).toBeTruthy()
  expect(screen.queryByText("Допуск счёта подтверждён последним снимком Core.")).toBeNull()
  expect(screen.getByText("Допуск не подтверждён; требуется проверка оператора.")).toBeTruthy()
})

it.each(["STOP_REQUIRED", "FUTURE_REASON"])("keeps an ARMED view blocked when denial reason %s is present", async (reason) => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse({ ...armedAdmissionView(), effective_denial_reasons: [reason] }))
    .mockResolvedValueOnce(preparationResponse()))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText("ARMED")).toBeTruthy()
  expect(screen.getByText(new RegExp(`${reason}:`))).toBeTruthy()
  expect(screen.queryByText("Допуск счёта подтверждён последним снимком Core.")).toBeNull()
})

it("expires preparation on its deadline without treating it as active trading readiness", async () => {
  vi.useRealTimers()
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  const slowRefresh = new Promise<Response>(() => undefined)
  const prepared = preparedView({ reconciliation_valid_until: "2026-10-14T10:00:00.000Z" })
  const admissionForExpiry = armableAdmissionView()
  admissionForExpiry.reconciliation = { ...admissionForExpiry.reconciliation, valid_until: "2026-10-14T10:00:00.000Z" }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(admissionResponse(admissionForExpiry))
    .mockResolvedValueOnce(preparationResponse(prepared))
    .mockReturnValue(slowRefresh)
  vi.stubGlobal("fetch", fetchMock)
  render(TradingAdmissionStatus, { props: props() })
  await waitFor(() => expect(screen.getByText("Готово только к явному ARM оператора.")).toBeTruthy())

  await vi.advanceTimersByTimeAsync(15_000)
  expect(screen.getByText("Срок подготовки истёк; требуется новая сверка перед ARM.")).toBeTruthy()
  expect(screen.queryByText("Готово только к явному ARM оператора.")).toBeNull()
})

it.each([
  ["account stop was revoked", { ...admission, admission_status: "STOP_REQUIRED", recovery_only: true, recovery_runtime_instance_id: runtimeId }, preparedView()],
  ["owner transferred", { ...armableAdmissionView(), owner_runtime_instance_id: brokerA, owner_generation: 3, reconciliation: { ...armableAdmissionView().reconciliation, owner_runtime_instance_id: brokerA, owner_generation: 3 } }, preparedView()],
  ["baseline changed", { ...armableAdmissionView(), baseline: { ...admission.baseline, baseline_id: brokerA } }, preparedView()],
  ["reconciliation changed", armableAdmissionView(), preparedView({ reconciliation_id: brokerA })],
  ["configuration fingerprint changed", armableAdmissionView(), preparedView({ configuration_fingerprint: "b".repeat(64) })],
  ["canary differs", armableAdmissionView(), preparedView({ suggested_limits: { ...preparedView().suggested_limits, canary_uid: "other-uid" } })],
  ["lease expired", { ...armableAdmissionView(), lease_expires_at: "2026-10-14T09:59:44.000Z" }, preparedView()],
  ["preparation was captured in the future", armableAdmissionView(), preparedView({ captured_at: "2026-10-14T10:00:00.000Z" })],
])("does not present PREPARED as arm-ready when %s", async (_name, accountView, preparationView) => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse(accountView))
    .mockResolvedValueOnce(preparationResponse(preparationView)))
  render(TradingAdmissionStatus, { props: props() })

  await waitFor(() => expect(screen.getByText(/Подготовка не совпадает|Время снимка/)).toBeTruthy())
  expect(screen.queryByText("Готово только к явному ARM оператора.")).toBeNull()
})

it.each([
  ["known preparation risk", preparedView({ risk_reasons: ["STALE_MARKET"] }), armableAdmissionView()],
  ["unknown preparation risk", preparedView({ risk_reasons: ["FUTURE_REASON"] }), armableAdmissionView()],
  ["reconciliation risk", preparedView(), {
    ...armableAdmissionView(),
    reconciliation: { ...armableAdmissionView().reconciliation, reasons: ["STALE_MARKET"] },
  }],
  ["reconciliation is DATA_UNKNOWN even when can_arm is true", preparedView(), {
    ...armableAdmissionView(),
    reconciliation: { ...armableAdmissionView().reconciliation, status: "DATA_UNKNOWN", can_arm: true, reasons: [] },
  }],
])("does not show PREPARED as ARM-ready with %s", async (_name, preparationView, accountView) => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse(accountView))
    .mockResolvedValueOnce(preparationResponse(preparationView)))
  render(TradingAdmissionStatus, { props: props() })

  expect(await screen.findByText(/Подготовка не совпадает/)).toBeTruthy()
  expect(screen.queryByText("Готово только к явному ARM оператора.")).toBeNull()
})

it("updates account lease, ARM and reconciliation expiry on the clock", async () => {
  vi.useRealTimers()
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  const ready = {
    ...armedAdmissionView(),
    lease_expires_at: "2026-10-14T09:59:50.000Z",
    arm_expires_at: "2026-10-14T09:59:55.000Z",
    recovery_only: false,
    effective_can_submit: true,
    effective_denial_reasons: [],
    reconciliation: { ...admission.reconciliation, status: "CANARY_READY", valid_until: "2026-10-14T09:59:57.000Z", can_arm: true, reasons: [] },
  }
  const slowRefresh = new Promise<Response>(() => undefined)
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(admissionResponse(ready))
    .mockResolvedValueOnce(new Response(null, { status: 404 }))
    .mockReturnValue(slowRefresh))
  render(TradingAdmissionStatus, { props: props() })
  expect(await screen.findByText("Допуск счёта подтверждён последним снимком Core.")).toBeTruthy()

  await vi.advanceTimersByTimeAsync(5000)
  expect(screen.queryByText("Допуск счёта подтверждён последним снимком Core.")).toBeNull()
  expect(screen.getByText(/LEASE_EXPIRED:/)).toBeTruthy()
  await vi.advanceTimersByTimeAsync(5000)
  expect(screen.getByText(/DISARMED:/)).toBeTruthy()
  await vi.advanceTimersByTimeAsync(2000)
  expect(screen.getByText(/Сверка истекла/)).toBeTruthy()
})

it("ignores an admission response from the previous broker after selection changes", async () => {
  let resolveOld!: (response: Response) => void
  const oldAdmission = new Promise<Response>((resolve) => { resolveOld = resolve })
  const fetchMock = vi.fn((url: string) => {
    if (url.includes(`${brokerA}/trading-admission/preparation`)) return Promise.resolve(preparationResponse())
    if (url.includes(`${brokerB}/trading-admission/preparation`)) return Promise.resolve(preparationResponse({ ...preparation, moscow_day: "2026-10-15" }))
    if (url.includes(`${brokerA}/trading-admission`)) return oldAdmission
    if (url.includes(`${brokerB}/trading-admission`)) return Promise.resolve(admissionResponse({ ...admission, broker_id: brokerB, account_id: "account-B", admission_status: "DISARMED", baseline: null, reconciliation: null, effective_denial_reasons: ["BASELINE_REQUIRED"] }))
    throw new Error(`Unexpected URL ${url}`)
  })
  vi.stubGlobal("fetch", fetchMock)
  const view = render(TradingAdmissionStatus, { props: props(brokerA, "account-A") })
  await view.rerender(props(brokerB, "account-B"))

  expect(await screen.findByText("DISARMED")).toBeTruthy()
  resolveOld(admissionResponse({ ...admission, broker_id: brokerA, account_id: "account-A", baseline: null, reconciliation: null, effective_can_submit: true, effective_denial_reasons: [] }))
  await waitFor(() => expect(screen.getByText("Счёт: account-B")).toBeTruthy())
  expect(screen.queryByText("Счёт: account-A")).toBeNull()
})
