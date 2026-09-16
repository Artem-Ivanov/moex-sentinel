import { cleanup, fireEvent, render, screen } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { flushPromises } from "@vue/test-utils"
import { afterEach, expect, it, vi } from "vitest"

import PositionDetailsView from "./PositionDetailsView.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

async function renderDetailsView() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: { template: "<div>positions</div>" } },
    { path: "/positions/:id", name: "position-details", component: PositionDetailsView },
  ] })
  await router.push("/positions/auto-1"); await router.isReady()
  return render(PositionDetailsView, { global: { plugins: [router] } })
}

it("does not expose automation controls on the read-only details page", async () => {
  const fetchMock = vi.fn().mockResolvedValue(Response.json(details))
  vi.stubGlobal("fetch", fetchMock)
  await renderDetailsView()
  await screen.findByRole("heading", { name: "SBER — торговый автомат" })

  expect(screen.queryByRole("button", { name: "Hold" })).toBeNull()
  expect(screen.queryByRole("button", { name: "Resume" })).toBeNull()
  expect(screen.queryByRole("button", { name: "Закрыть автомат" })).toBeNull()
})

const automation = {
  id: "auto-1", broker_id: "b1", broker_name: "Sandbox", account_id: "a1",
  instrument_id: "i1", ticker: "SBER", instrument_name: "Sber", state: "IN_WORK",
  suspended_from_state: null, revision: 2, last_sequence_number: 3, resume_requested: false,
  quantity_lots: 2, average_price: "100", invested_amount: "200", realized_pnl: "0",
  unrealized_pnl: "4", net_pnl: "3.5", actual_commissions: "0.5",
  currency: "RUB", strategy_code: "adaptive_scalping", strategy_version: "1",
}

const details = { automation, operations: [], candles: [{
  instrument_id: "i1", open: "100", high: "101", low: "99", close: "100.5",
  volume: 10, started_at: "2026-08-06T13:59:00Z", is_complete: true,
}], errors: [] }

it("shows automation state and trading details", async () => {
  const fetchMock = vi.fn().mockResolvedValue(Response.json(details))
  vi.stubGlobal("fetch", fetchMock)
  await renderDetailsView()

  expect(await screen.findByRole("heading", { name: "SBER — торговый автомат" })).toBeTruthy()
  expect(screen.getByText("IN_WORK")).toBeTruthy()
  expect(screen.getByText("Минутные свечи за последние два часа")).toBeTruthy()
  expect(screen.getByRole("img", { name: "Минутные свечи за последние два часа" })).toBeTruthy()
  expect(document.querySelector(".candlestick-chart__current-price-label")?.textContent).toBe("100.50")
  expect(document.querySelector(".candlestick-chart__average-price-label")?.textContent).toBe("100.00")
  expect(screen.queryByText(/Последняя:/)).toBeNull()
  expect(screen.queryByText(/Средняя:/)).toBeNull()
  expect(document.querySelector(".candlestick-chart__average-price-line")).toBeTruthy()
})

it("does not show an average marker for an empty position average price", async () => {
  const invalidDetails = { ...details, automation: { ...automation, average_price: "" } }
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(invalidDetails)))
  await renderDetailsView()
  await screen.findByRole("img", { name: "Минутные свечи за последние два часа" })

  expect(document.querySelector(".candlestick-chart__average-price-line")).toBeNull()
})

it("refreshes once per minute and clears the timer on unmount", async () => {
  vi.useFakeTimers()
  const fetchMock = vi.fn().mockResolvedValue(Response.json(details))
  vi.stubGlobal("fetch", fetchMock)
  const rendered = await renderDetailsView()
  await flushPromises()
  expect(fetchMock).toHaveBeenCalledTimes(1)

  await vi.advanceTimersByTimeAsync(60_000)
  await flushPromises()
  expect(fetchMock).toHaveBeenCalledTimes(2)

  rendered.unmount()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  vi.useRealTimers()
})

it("shows a candle loading error without claiming that the period has no candles", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
    ...details,
    candles: [],
    errors: [{ source: "candles", code: "MARKET_CANDLES_UNAVAILABLE", message: "Не удалось получить минутные свечи." }],
  })))
  await renderDetailsView()

  expect(await screen.findByText("Не удалось получить минутные свечи.")).toBeTruthy()
  expect(screen.queryByText("Завершённых свечей за последние два часа нет")).toBeNull()
})

it("does not start polling after unmount while the initial request is pending", async () => {
  vi.useFakeTimers()
  let resolveRequest!: (response: Response) => void
  const pending = new Promise<Response>((resolve) => { resolveRequest = resolve })
  const fetchMock = vi.fn().mockReturnValue(pending)
  vi.stubGlobal("fetch", fetchMock)
  const rendered = await renderDetailsView()

  rendered.unmount()
  resolveRequest(Response.json(details))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(60_000)

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})


it("reports operation failures and restores exact trade rows on manual refresh", async () => {
  const operationError = { source: "operations", code: "BROKER_UNAVAILABLE", message: "Не удалось получить операции." }
  const operations = [
    { operation_id: "buy-1", operation_type: "BUY", state: "EXECUTED", occurred_at: "2026-08-06T13:59:00Z", quantity: "1", price: { amount: "553.850000000", currency: "RUB" }, payment: { amount: "-553.850000000", currency: "RUB" }, commission: { amount: "0.276925000", currency: "RUB" } },
    { operation_id: "sell-1", operation_type: "SELL", state: "EXECUTED", occurred_at: "2026-08-06T14:00:00Z", quantity: "2", price: { amount: "554.150000000", currency: "RUB" }, payment: { amount: "1108.300000000", currency: "RUB" }, commission: { amount: "0.554150000", currency: "RUB" } },
  ]
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ ...details, errors: [operationError] }))
    .mockResolvedValueOnce(Response.json({ ...details, operations, errors: [operationError] }))
    .mockResolvedValueOnce(Response.json({ ...details, operations }))
  vi.stubGlobal("fetch", fetchMock)
  await renderDetailsView()

  expect(await screen.findByText(operationError.message)).toBeTruthy()
  expect(screen.queryByText("Исполненных операций пока нет")).toBeNull()
  await fireEvent.click(screen.getByRole("button", { name: "Обновить" }))
  expect(await screen.findByRole("row", { name: /BUY EXECUTED 1 553\.850000000 RUB -553\.850000000 RUB 0\.276925000 RUB/ })).toBeTruthy()
  expect(screen.getByRole("row", { name: /SELL EXECUTED 2 554\.150000000 RUB 1108\.300000000 RUB 0\.554150000 RUB/ })).toBeTruthy()
  expect(screen.getByText(operationError.message)).toBeTruthy()
  await flushPromises()
  await fireEvent.click(screen.getByRole("button", { name: "Обновить" }))
  await flushPromises()
  expect(screen.queryByText(operationError.message)).toBeNull()
  expect(screen.getAllByRole("row")).toHaveLength(3)
  expect(fetchMock.mock.calls).toEqual(Array.from({ length: 3 }, () => ["/api/trading-automations/auto-1/details", undefined]))
})
