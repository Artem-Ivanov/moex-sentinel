import { cleanup, fireEvent, render, screen } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, expect, it, vi } from "vitest"

import PositionsListView from "./PositionsListView.vue"
import { runtime } from "../runtime"

const brokerA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
const brokerB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

const summary = {
  captured_at: null,
  currencies: [],
  errors: [],
}

function automation(id: string, brokerId: string, accountId: string) {
  return {
    id, broker_id: brokerId, broker_name: brokerId, account_id: accountId, instrument_id: "uid-1", ticker: id,
    instrument_name: id, state: "HOLD", suspended_from_state: "IN_WORK", revision: 1, last_sequence_number: 0,
    resume_requested: true, currency: "RUB", strategy_code: "adaptive_scalping", strategy_version: "1",
    quantity_lots: 1, average_price: "10", invested_amount: "10", realized_pnl: "0", unrealized_pnl: "0",
    net_pnl: "0", actual_commissions: "0",
  }
}

afterEach(() => {
  cleanup()
  runtime.value = null
  vi.unstubAllGlobals()
})

it("loads read-only account status only for the selected position broker and keeps HOLD separate", async () => {
  runtime.value = { environment: "PROD", access_mode: "TRADE" }
  const fetchMock = vi.fn((url: string) => {
    if (url === "/api/trading/summary") return Promise.resolve(Response.json(summary))
    if (url.startsWith("/api/operations")) return Promise.resolve(Response.json({ items: [], errors: [] }))
    if (url === "/api/trading-automations") return Promise.resolve(Response.json({ items: [
      automation("auto-A", brokerA, "account-A"), automation("auto-B", brokerB, "account-B"),
    ] }))
    if (url === `/api/brokers/${brokerA}/trading-admission`) return Promise.resolve(Response.json({ broker_id: brokerA, account_id: "account-A", revision: 1, owner_runtime_instance_id: null, owner_generation: null, recovery_runtime_instance_id: null, admission_status: "STOP_REQUIRED", arm_revision: null, lease_expires_at: null, arm_expires_at: null, limits: null, configuration_fingerprint: null, baseline: null, reconciliation: null, effective_can_submit: false, effective_denial_reasons: ["STOP_REQUIRED"], unresolved_reservation_count: 0, outstanding_order_count: 0, recovery_only: true }))
    if (url === `/api/brokers/${brokerA}/trading-admission/preparation`) return Promise.resolve(new Response(null, { status: 404 }))
    if (url === `/api/brokers/${brokerB}/trading-admission`) return Promise.resolve(Response.json({ broker_id: brokerB, account_id: "account-B", revision: 1, owner_runtime_instance_id: null, owner_generation: null, recovery_runtime_instance_id: null, admission_status: "DISARMED", arm_revision: null, lease_expires_at: null, arm_expires_at: null, limits: null, configuration_fingerprint: null, baseline: null, reconciliation: null, effective_can_submit: false, effective_denial_reasons: ["BASELINE_REQUIRED"], unresolved_reservation_count: 0, outstanding_order_count: 0, recovery_only: false }))
    if (url === `/api/brokers/${brokerB}/trading-admission/preparation`) return Promise.resolve(new Response(null, { status: 404 }))
    throw new Error(`Unexpected URL ${url}`)
  })
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/positions", component: PositionsListView }] })
  await router.push("/positions")
  await router.isReady()
  render(PositionsListView, { global: { plugins: [router] } })

  const rowA = await screen.findByRole("row", { name: new RegExp(`auto-A ${brokerA} account-A HOLD`) })
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("trading-admission"))).toBe(false)
  await fireEvent.click(rowA)
  expect(await screen.findByText("Запрос Resume принят.")).toBeTruthy()
  expect(screen.getAllByText("HOLD").length).toBeGreaterThan(0)
  expect(await screen.findByText("STOP_REQUIRED")).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledWith(`/api/brokers/${brokerA}/trading-admission`, expect.objectContaining({ signal: expect.anything() }))
  expect(fetchMock).toHaveBeenCalledWith(`/api/brokers/${brokerA}/trading-admission/preparation`, expect.objectContaining({ signal: expect.anything() }))

  await fireEvent.click(screen.getByRole("row", { name: new RegExp(`auto-B ${brokerB} account-B HOLD`) }))
  expect(await screen.findByText("Счёт: account-B")).toBeTruthy()
  expect(screen.queryByText("Счёт: account-A")).toBeNull()
})

it.each([
  ["TEST", "TEST"],
  ["unknown", null],
])("does not request production admission outside confirmed PROD runtime: %s", async (_label, environment) => {
  runtime.value = environment === null ? null : { environment: environment as "TEST" | "PROD", access_mode: "READ_ONLY" }
  const fetchMock = vi.fn((url: string) => {
    if (url === "/api/trading/summary") return Promise.resolve(Response.json(summary))
    if (url.startsWith("/api/operations")) return Promise.resolve(Response.json({ items: [], errors: [] }))
    return Promise.resolve(Response.json({ items: [automation("auto-A", brokerA, "account-A")] }))
  })
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/positions", component: PositionsListView }] })
  await router.push("/positions")
  await router.isReady()
  render(PositionsListView, { global: { plugins: [router] } })

  const row = await screen.findByRole("row", { name: new RegExp(`auto-A ${brokerA} account-A HOLD`) })
  await fireEvent.click(row)
  expect(screen.queryByRole("region", { name: "Допуск счёта" })).toBeNull()
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("trading-admission"))).toBe(false)
})
