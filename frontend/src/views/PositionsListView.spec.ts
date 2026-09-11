import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, expect, it, vi } from "vitest"

import PositionsListView from "./PositionsListView.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const summary = {
  captured_at: "2026-08-15T10:00:00Z",
  currencies: [{
    currency: "RUB",
    portfolio_value: "150.00",
    free_cash: "25.00",
    pnl_24h: { value: "7.00", from: "2026-08-14T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: true },
    pnl_7d: { value: "12.00", from: "2026-08-08T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: true },
    pnl_30d: { value: "15.00", from: "2026-07-16T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: true },
  }],
  errors: [],
}

it("renders automations as an interactive table and opens selected details", async () => {
  const fetchMock = vi.fn().mockImplementation((url: string) => Response.json(
    url === "/api/trading/summary"
      ? summary
      : url.startsWith("/api/operations")
        ? { items: [], errors: [] }
        : { items: [{ id: "auto-1", broker_id: "b1", broker_name: "Broker", account_id: "a1", instrument_id: "i1", ticker: "TEST", instrument_name: "Test", state: "IN_WORK", suspended_from_state: null, revision: 2, last_sequence_number: 3, resume_requested: false, currency: "RUB", strategy_code: "adaptive_scalping", strategy_version: "1", quantity_lots: 2, average_price: "10", invested_amount: "20", realized_pnl: "1", unrealized_pnl: "2", net_pnl: "2.5", actual_commissions: "0.5" }] },
  ))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/positions", name: "positions", component: PositionsListView }, { path: "/positions/:id", name: "position-details", component: { template: "<div>details</div>" } }] })
  await router.push({ name: "positions" }); await router.isReady()
  render(PositionsListView, { global: { plugins: [router] } })

  expect(await screen.findByRole("columnheader", { name: "Тикер" })).toBeTruthy()
  expect(screen.getByRole("heading", { name: "Торговля" })).toBeTruthy()
  expect(screen.getByRole("heading", { name: "Сводка" })).toBeTruthy()
  expect(screen.getByRole("columnheader", { name: "Средняя цена" })).toBeTruthy()
  expect(await screen.findByRole("heading", { name: "Последние операции" })).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledWith("/api/trading-automations", undefined)
  expect(fetchMock).toHaveBeenCalledWith("/api/trading/summary")
  expect(fetchMock).toHaveBeenCalledWith("/api/operations?limit=20")
  const row = screen.getByRole("row", { name: /TEST Broker a1 IN_WORK 2/ })
  await fireEvent.click(row)
  await fireEvent.click(screen.getByRole("button", { name: "Подробнее" }))

  await waitFor(() => expect(router.currentRoute.value.name).toBe("position-details"))
  expect(router.currentRoute.value.params.id).toBe("auto-1")
})

it("refreshes positions and operations without reloading the page", async () => {
  const fetchMock = vi.fn().mockImplementation((url: string) => Response.json(
    url === "/api/trading/summary"
      ? summary
      : url.startsWith("/api/operations") ? { items: [], errors: [] } : { items: [] },
  ))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: PositionsListView },
    { path: "/positions/:id", name: "position-details", component: { template: "<div>details</div>" } },
  ] })
  await router.push({ name: "positions" }); await router.isReady()
  render(PositionsListView, { global: { plugins: [router] } })
  await screen.findByRole("heading", { name: "Последние операции" })

  const refresh = screen.getByRole("button", { name: "Обновить" })
  await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false))
  await fireEvent.click(refresh)
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(6))
  expect(fetchMock).toHaveBeenCalledWith("/api/trading-automations", undefined)
  expect(fetchMock).toHaveBeenCalledWith("/api/operations?limit=20")
  expect(fetchMock).toHaveBeenCalledWith("/api/trading/summary")
})

it("enables controls for the selected state and keeps selection after hold reload", async () => {
  const active = { id: "auto-1", broker_id: "b1", broker_name: "Broker", account_id: "a1", instrument_id: "i1", ticker: "TEST", instrument_name: "Test", state: "IN_WORK", suspended_from_state: null, revision: 2, last_sequence_number: 3, resume_requested: false, currency: "RUB", strategy_code: "adaptive_scalping", strategy_version: "1", quantity_lots: 2, average_price: "10", invested_amount: "20", realized_pnl: "1", unrealized_pnl: "2", net_pnl: "2.5", actual_commissions: "0.5" }
  const held = { ...active, state: "HOLD", suspended_from_state: "IN_WORK", revision: 3 }
  let listCalls = 0
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === "/api/trading/summary") return Response.json(summary)
    if (url.startsWith("/api/operations")) return Response.json({ items: [], errors: [] })
    if (url === "/api/trading-automations/auto-1/hold") return Response.json(held)
    if (url === "/api/trading-automations") {
      listCalls += 1
      return Response.json({ items: [listCalls === 1 ? active : held] })
    }
    throw new Error(`Unexpected URL ${url}`)
  })
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: PositionsListView },
    { path: "/positions/:id", name: "position-details", component: { template: "<div>details</div>" } },
  ] })
  await router.push({ name: "positions" }); await router.isReady()
  render(PositionsListView, { global: { plugins: [router] } })

  const hold = screen.getByRole("button", { name: "Hold" })
  const resume = screen.getByRole("button", { name: "Resume" })
  const close = screen.getByRole("button", { name: "Закрыть автомат" })
  expect((hold as HTMLButtonElement).disabled).toBe(true)
  expect((resume as HTMLButtonElement).disabled).toBe(true)
  expect((close as HTMLButtonElement).disabled).toBe(true)

  await fireEvent.click(await screen.findByRole("row", { name: /TEST Broker a1 IN_WORK 2/ }))
  expect((hold as HTMLButtonElement).disabled).toBe(false)
  expect((resume as HTMLButtonElement).disabled).toBe(true)
  expect((close as HTMLButtonElement).disabled).toBe(false)
  await fireEvent.click(hold)

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/trading-automations/auto-1/hold",
    expect.objectContaining({ method: "POST" }),
  ))
  await waitFor(() => expect((resume as HTMLButtonElement).disabled).toBe(false))
  expect(screen.getByRole("row", { name: /TEST Broker a1 HOLD 2/ }).className).toContain("data-table__row--selected")
  expect((hold as HTMLButtonElement).disabled).toBe(true)
})

it("keeps successful blocks when summary refresh fails", async () => {
  let summaryCalls = 0
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === "/api/trading/summary") {
      summaryCalls += 1
      return summaryCalls === 1 ? Response.json(summary) : Promise.reject(new Error("offline"))
    }
    if (url.startsWith("/api/operations")) return Response.json({ items: [], errors: [] })
    return Response.json({ items: [] })
  })
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: PositionsListView },
    { path: "/positions/:id", name: "position-details", component: { template: "<div>details</div>" } },
  ] })
  await router.push({ name: "positions" }); await router.isReady()
  render(PositionsListView, { global: { plugins: [router] } })

  expect(await screen.findByRole("heading", { name: "RUB" })).toBeTruthy()
  const refresh = screen.getByRole("button", { name: "Обновить" })
  await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false))
  await fireEvent.click(refresh)

  expect(await screen.findByText("Не удалось загрузить сводку.")).toBeTruthy()
  expect(screen.getByRole("heading", { name: "RUB" })).toBeTruthy()
  expect(screen.getByText("Активных автоматов нет")).toBeTruthy()
  expect(screen.getByText("Операции отсутствуют")).toBeTruthy()
})
