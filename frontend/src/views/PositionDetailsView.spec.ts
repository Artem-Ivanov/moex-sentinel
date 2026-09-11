import { cleanup, render, screen } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { flushPromises } from "@vue/test-utils"
import { afterEach, expect, it, vi } from "vitest"

import PositionDetailsView from "./PositionDetailsView.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it("does not expose automation controls on the read-only details page", async () => {
  const fetchMock = vi.fn().mockResolvedValue(Response.json(details))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: { template: "<div>positions</div>" } },
    { path: "/positions/:id", name: "position-details", component: PositionDetailsView },
  ] })
  await router.push("/positions/auto-1"); await router.isReady()
  render(PositionDetailsView, { global: { plugins: [router] } })
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
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: { template: "<div>positions</div>" } },
    { path: "/positions/:id", name: "position-details", component: PositionDetailsView },
  ] })
  await router.push("/positions/auto-1"); await router.isReady()
  render(PositionDetailsView, { global: { plugins: [router] } })

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
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: { template: "<div>positions</div>" } },
    { path: "/positions/:id", name: "position-details", component: PositionDetailsView },
  ] })
  await router.push("/positions/auto-1"); await router.isReady()
  render(PositionDetailsView, { global: { plugins: [router] } })
  await screen.findByRole("img", { name: "Минутные свечи за последние два часа" })

  expect(document.querySelector(".candlestick-chart__average-price-line")).toBeNull()
})

it("refreshes once per minute and clears the timer on unmount", async () => {
  vi.useFakeTimers()
  const fetchMock = vi.fn().mockResolvedValue(Response.json(details))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: { template: "<div>positions</div>" } },
    { path: "/positions/:id", name: "position-details", component: PositionDetailsView },
  ] })
  await router.push("/positions/auto-1"); await router.isReady()
  const rendered = render(PositionDetailsView, { global: { plugins: [router] } })
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
