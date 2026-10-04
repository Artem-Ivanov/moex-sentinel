import { runtime } from "../runtime"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/vue"
import { createMemoryHistory, createRouter, RouterView } from "vue-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import PositionsListView from "./PositionsListView.vue"

beforeEach(() => { runtime.value = { environment: "TEST", access_mode: "TRADE" } })

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

const heldAutomation = {
  id: "held-1", broker_id: "b1", broker_name: "Broker", account_id: "a1", instrument_id: "i1", ticker: "HELD", instrument_name: "Held",
  state: "HOLD", suspended_from_state: "IN_WORK", revision: 1, last_sequence_number: 0, resume_requested: false,
  currency: "RUB", strategy_code: "adaptive_scalping", strategy_version: "1", quantity_lots: 2, average_price: "10", invested_amount: "20",
  realized_pnl: "1", unrealized_pnl: "2", net_pnl: "2.5", actual_commissions: "0.5",
}

async function renderHeldPosition(overrides = {}) {
  const automation = { ...heldAutomation, ...overrides }
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === "/api/trading/summary") return Response.json(summary)
    if (url.startsWith("/api/operations")) return Response.json({ items: [], errors: [] })
    if (url.endsWith("/resume")) return Response.json({ ...automation, state: "IN_QUEUE" })
    return Response.json({ items: [automation] })
  })
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/positions", name: "positions", component: PositionsListView }] })
  await router.push("/positions"); await router.isReady()
  render(PositionsListView, { global: { plugins: [router] } })
  const row = await screen.findByRole("row", { name: /HELD Broker a1/ })
  await waitFor(() => expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(false))
  return { row, fetchMock }
}

it.each([false, true])("explains READ_ONLY resume blocking and shows only confirmed financial values: pending=%s", async (pending) => {
  runtime.value = { environment: "PROD", access_mode: "READ_ONLY" }
  const { row, fetchMock } = await renderHeldPosition({ bootstrap_pending: pending })
  if (pending) {
    expect(within(row).getByText("Ожидает первоначальной сверки позиции")).toBeTruthy()
    expect(within(row).getAllByRole("cell").slice(4).map(cell => cell.textContent)).toEqual(["—", "—", "—", "—", "—"])
  } else {
    expect(within(row).queryByText("Ожидает первоначальной сверки позиции")).toBeNull()
    expect(within(row).getByText("10 RUB")).toBeTruthy()
  }
  await fireEvent.click(row)
  const resume = screen.getByRole("button", { name: "Resume" }) as HTMLButtonElement
  expect(resume.disabled).toBe(true)
  const reason = screen.getByText("Возобновление — торговая команда, недоступная в режиме только чтения.")
  expect(resume.getAttribute("aria-describedby")).toBe(reason.id)
  await fireEvent.click(resume)
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/resume"))).toBe(false)
})

it.each([{}, { bootstrap_pending: true }])("resumes a TEST TRADE HOLD once, including an unfinished bootstrap: %j", async (overrides) => {
  const { row, fetchMock } = await renderHeldPosition(overrides)
  expect(screen.getByText("Выберите автомат для возобновления.")).toBeTruthy()
  await fireEvent.click(row)
  const resume = screen.getByRole("button", { name: "Resume" }) as HTMLButtonElement
  expect(resume.disabled).toBe(false)
  await fireEvent.click(resume)
  await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/resume"))).toHaveLength(1))
})

it.each([
  [{ state: "IN_WORK" }, "Возобновление доступно только для автомата в HOLD."],
  [{ resume_requested: true }, "Запрос на возобновление уже принят."],
])("explains unavailable resume without inferring bootstrap from HOLD or sequence zero: %j", async (overrides, reason) => {
  const { row } = await renderHeldPosition(overrides)
  await fireEvent.click(row)
  expect(screen.getByText(reason)).toBeTruthy()
  expect((screen.getByRole("button", { name: "Resume" }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByText("Ожидает первоначальной сверки позиции")).toBeNull()
  expect(within(row).getByText("10 RUB")).toBeTruthy()
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function operationsResponse(ticker: string) {
  return {
    items: [{
      occurred_at: "2026-08-15T10:00:00Z", broker_id: "b1", broker_name: "Broker", account_id: "a1",
      operation_id: ticker, ticker, operation_type: "BUY", state: "DONE", quantity: "1",
      price: { amount: "10", currency: "RUB" }, payment: { amount: "10", currency: "RUB" },
      commission: { amount: "0", currency: "RUB" },
    }], errors: [],
  }
}

async function renderOperationsWithDeferredFetch(onOperationsFetch: (url: string) => Promise<Response>) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === "/api/trading/summary") return Response.json(summary)
    if (url.startsWith("/api/operations")) return onOperationsFetch(url)
    return Response.json({ items: [] })
  })
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/positions", name: "positions", component: PositionsListView }] })
  await router.push("/positions"); await router.isReady()
  const view = render(PositionsListView, { global: { plugins: [router] } })
  await screen.findByRole("heading", { name: "Последние операции" })
  return { fetchMock, unmount: view.unmount }
}

it("keeps the newest limit result when an older operations request resolves last", async () => {
  const first = deferred<Response>()
  const second = deferred<Response>()
  const { fetchMock } = await renderOperationsWithDeferredFetch((url) => url.endsWith("limit=20") ? first.promise : second.promise)
  const limit = screen.getByRole("combobox", { name: "Количество операций" })
  await fireEvent.update(limit, "50")
  second.resolve(Response.json(operationsResponse("NEWEST")))
  expect(await screen.findByText("NEWEST")).toBeTruthy()
  first.resolve(Response.json(operationsResponse("STALE")))
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(screen.queryByText("STALE")).toBeNull()
  expect(screen.getByText("NEWEST")).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledWith("/api/operations?limit=20")
  expect(fetchMock).toHaveBeenCalledWith("/api/operations?limit=50")
})

it("keeps the newest operations error when an older request succeeds later", async () => {
  const first = deferred<Response>()
  const second = deferred<Response>()
  await renderOperationsWithDeferredFetch((url) => url.endsWith("limit=20") ? first.promise : second.promise)
  await fireEvent.update(screen.getByRole("combobox", { name: "Количество операций" }), "50")
  second.reject(new Error("newest failed"))
  expect(await screen.findByText("Не удалось загрузить операции.")).toBeTruthy()
  first.resolve(Response.json(operationsResponse("STALE")))
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(screen.queryByText("STALE")).toBeNull()
  expect(screen.getByText("Не удалось загрузить операции.")).toBeTruthy()
})

it("ignores an older failure after the newest operations request succeeds", async () => {
  const first = deferred<Response>()
  const second = deferred<Response>()
  await renderOperationsWithDeferredFetch((url) => url.endsWith("limit=20") ? first.promise : second.promise)
  await fireEvent.update(screen.getByRole("combobox", { name: "Количество операций" }), "50")
  second.resolve(Response.json(operationsResponse("NEWEST")))
  expect(await screen.findByText("NEWEST")).toBeTruthy()
  first.reject(new Error("older failed"))
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(screen.queryByText("Не удалось загрузить операции.")).toBeNull()
  expect(screen.getByText("NEWEST")).toBeTruthy()
})

it.each(["success", "error"] as const)("keeps the new operations view stable when an old %s completes after navigation", async (oldResult) => {
  const oldRequest = deferred<Response>()
  const latestRequest = deferred<Response>()
  let operationsCalls = 0
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url === "/api/trading/summary") return Response.json(summary)
    if (url.startsWith("/api/operations")) {
      operationsCalls += 1
      if (operationsCalls === 1) return oldRequest.promise
      if (url.endsWith("limit=50")) return latestRequest.promise
      return Response.json({ items: [], errors: [] })
    }
    return Response.json({ items: [] })
  })
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/positions", name: "positions", component: PositionsListView },
    { path: "/away", name: "away", component: { template: "<div>away</div>" } },
  ] })
  await router.push("/positions"); await router.isReady()
  render(RouterView, { global: { plugins: [router] } })
  await screen.findByRole("heading", { name: "Последние операции" })

  await router.push("/away")
  expect(await screen.findByText("away")).toBeTruthy()
  await router.push("/positions")
  await screen.findByRole("heading", { name: "Последние операции" })
  await fireEvent.update(screen.getByRole("combobox", { name: "Количество операций" }), "50")
  latestRequest.resolve(Response.json(operationsResponse("CURRENT")))
  expect(await screen.findByText("CURRENT")).toBeTruthy()

  if (oldResult === "success") oldRequest.resolve(Response.json(operationsResponse("STALE")))
  else oldRequest.reject(new Error("old view failed"))
  await new Promise((resolve) => setTimeout(resolve, 0))

  expect((screen.getByRole("combobox", { name: "Количество операций" }) as HTMLSelectElement).value).toBe("50")
  expect(screen.getByText("CURRENT")).toBeTruthy()
  expect(screen.queryByText("STALE")).toBeNull()
  expect(screen.queryByText("Не удалось загрузить операции.")).toBeNull()
})
