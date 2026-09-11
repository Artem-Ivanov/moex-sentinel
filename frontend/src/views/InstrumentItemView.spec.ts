import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { flushPromises } from "@vue/test-utils"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import InstrumentItemView from "./InstrumentItemView.vue"

const details = {
  broker_name: "Sandbox",
  instrument: { instrument_id: "uid-1", figi: "figi-1", ticker: "SBER", name: "Sber", class_code: "TQBR", instrument_type: "SHARE", category: "SHARE", currency: "RUB", lot: 10, api_trade_available: true, is_active: true, is_selected: true, first_seen_at: "2026-08-05T10:00:00Z", last_seen_at: "2026-08-05T12:00:00Z" },
  last_price: { price: "312.45", captured_at: "2026-08-05T12:00:00Z" },
  lot_price: "3124.50",
  sync_state: { broker_id: "broker-1", status: "SUCCESS", last_success_at: "2026-08-05T12:00:00Z", last_attempt_at: "2026-08-05T12:00:00Z", safe_error: null },
}

const candles = { items: [{
  instrument_id: "uid-1",
  open: "310",
  high: "313",
  low: "309.5",
  close: "312.45",
  volume: 1250,
  started_at: "2026-08-05T09:59:00Z",
  is_complete: true,
}] }

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-08-05T10:00:00Z")
})

afterEach(() => {
  cleanup()
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

async function renderView(): Promise<void> {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: { template: "<div>list</div>" } },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: InstrumentItemView },
    { path: "/positions/:id", name: "position-details", component: { template: "<div>position</div>" } },
  ] })
  await router.push("/instruments/broker-1/row-1")
  await router.isReady()
  render(InstrumentItemView, { global: { plugins: [router] } })
}

it("loads the last two hours of minute candles and renders future intervals disabled", async () => {
  vi.useFakeTimers({ toFake: ["Date"] })
  vi.setSystemTime("2026-08-05T10:00:00Z")
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(Response.json(candles))
  vi.stubGlobal("fetch", fetchMock)

  await renderView()

  expect(await screen.findByRole("heading", { name: "SBER — Sber" })).toBeTruthy()
  expect(await screen.findByRole("img", { name: "Минутные свечи за последние два часа" })).toBeTruthy()
  expect(screen.getByText("312.45 RUB")).toBeTruthy()
  expect(screen.getByText("Цена лота: 3124.50 RUB")).toBeTruthy()
  expect(screen.getByRole("button", { name: "1 мин" }).getAttribute("aria-pressed")).toBe("true")
  for (const label of ["5 мин", "15 мин", "1 час", "1 день"]) {
    expect((screen.getByRole("button", { name: label }) as HTMLButtonElement).disabled).toBe(true)
  }
  expect(fetchMock).toHaveBeenNthCalledWith(2,
    "/api/brokers/broker-1/market/instruments/uid-1/candles"
      + "?from=2026-08-05T08%3A00%3A00.000Z"
      + "&to=2026-08-05T10%3A00%3A00.000Z&interval=1_MIN",
  )
  expect(fetchMock).toHaveBeenNthCalledWith(
    1,
    "/api/brokers/broker-1/instruments/row-1",
  )
})

it("keeps instrument details visible when the last two hours have no candles", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(Response.json({ items: [] })))

  await renderView()

  expect(await screen.findByRole("heading", { name: "SBER — Sber" })).toBeTruthy()
  expect(await screen.findByText("За последние два часа завершённых свечей нет")).toBeTruthy()
})

it("isolates a candle request failure from the instrument card", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(new Response(null, { status: 503 })))

  await renderView()

  expect(await screen.findByRole("heading", { name: "SBER — Sber" })).toBeTruthy()
  expect(await screen.findByText("Не удалось загрузить свечи.")).toBeTruthy()
  expect(screen.getByText("312.45 RUB")).toBeTruthy()
})

it("shows cached candles immediately while refreshing them", async () => {
  sessionStorage.setItem("candles:broker-1:uid-1:1_MIN", JSON.stringify({
    version: 1,
    fetched_at: "2026-08-05T09:00:00.000Z",
    items: candles.items,
  }))
  const candleResponse = new Promise<Response>(() => undefined)
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockReturnValueOnce(candleResponse))

  await renderView()

  expect(await screen.findByRole("img", { name: "Минутные свечи за последние два часа" })).toBeTruthy()
  expect(screen.getByRole("status", { name: "Обновление свечей" })).toBeTruthy()
})

it("shows a loading status inside an empty chart area on the first request", async () => {
  const candleResponse = new Promise<Response>(() => undefined)
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockReturnValueOnce(candleResponse))

  await renderView()

  const status = await screen.findByRole("status", { name: "Обновление свечей" })
  expect(status.classList.contains("candle-loading-overlay")).toBe(true)
  expect(status.querySelector(".candle-spinner")).toBeTruthy()
  expect(status.closest(".candle-chart-frame")).toBeTruthy()
  expect(screen.queryByRole("img", { name: "Минутные свечи за последние два часа" })).toBeNull()
})

it("replaces cached candles after a successful refresh", async () => {
  sessionStorage.setItem("candles:broker-1:uid-1:1_MIN", JSON.stringify({
    version: 1,
    fetched_at: "2026-08-05T09:00:00.000Z",
    items: candles.items,
  }))
  let resolveCandles!: (response: Response) => void
  const candleResponse = new Promise<Response>((resolve) => { resolveCandles = resolve })
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockReturnValueOnce(candleResponse))

  await renderView()
  expect(await screen.findByRole("status", { name: "Обновление свечей" })).toBeTruthy()

  const refreshedItems = [{ ...candles.items[0], close: "315.10" }]
  resolveCandles(Response.json({ items: refreshedItems }))

  expect(await screen.findByRole("img", { name: "Минутные свечи за последние два часа" })).toBeTruthy()
  await vi.waitFor(() => {
    expect(screen.queryByRole("status", { name: "Обновление свечей" })).toBeNull()
  })
  const cached = JSON.parse(sessionStorage.getItem("candles:broker-1:uid-1:1_MIN") ?? "null")
  expect(cached.items).toEqual(refreshedItems)
})

it("keeps cached candles and reports a refresh error", async () => {
  const serialized = JSON.stringify({
    version: 1,
    fetched_at: "2026-08-05T09:00:00.000Z",
    items: candles.items,
  })
  sessionStorage.setItem("candles:broker-1:uid-1:1_MIN", serialized)
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(new Response(null, { status: 503 })))

  await renderView()

  expect(await screen.findByText("Не удалось обновить свечи.")).toBeTruthy()
  expect(screen.getByRole("img", { name: "Минутные свечи за последние два часа" })).toBeTruthy()
  expect(screen.queryByRole("status", { name: "Обновление свечей" })).toBeNull()
  expect(sessionStorage.getItem("candles:broker-1:uid-1:1_MIN")).toBe(serialized)
})

it("refreshes the rolling two-hour window every minute and stops after leaving", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-08-05T10:00:00Z")
  const refreshedItems = [{ ...candles.items[0], started_at: "2026-08-05T10:00:00Z", close: "315.10" }]
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(Response.json(candles))
    .mockResolvedValueOnce(Response.json({ items: refreshedItems }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()
  await flushPromises()

  await vi.advanceTimersByTimeAsync(60_000)
  await flushPromises()

  expect(fetchMock).toHaveBeenNthCalledWith(3,
    "/api/brokers/broker-1/market/instruments/uid-1/candles"
      + "?from=2026-08-05T08%3A01%3A00.000Z"
      + "&to=2026-08-05T10%3A01%3A00.000Z&interval=1_MIN",
  )
  expect(document.querySelector(".candlestick-chart__current-price-label")?.textContent).toBe("315.10")
  cleanup()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(fetchMock).toHaveBeenCalledTimes(3)
})

it("does not show cached candles outside the current two-hour window when refresh fails", async () => {
  const expiredItems = [{ ...candles.items[0], started_at: "2026-08-05T07:59:00Z" }]
  sessionStorage.setItem("candles:broker-1:uid-1:1_MIN", JSON.stringify({
    version: 1,
    fetched_at: "2026-08-05T08:00:00.000Z",
    items: expiredItems,
  }))
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(new Response(null, { status: 503 })))

  await renderView()

  expect(await screen.findByText("Не удалось обновить свечи.")).toBeTruthy()
  expect(screen.queryByRole("img", { name: "Минутные свечи за последние два часа" })).toBeNull()
})

it("renders all 120 completed minute candles including the start of the two-hour window", async () => {
  const completeWindow = Array.from({ length: 120 }, (_, minute) => ({
    ...candles.items[0],
    started_at: new Date(Date.parse("2026-08-05T08:00:00Z") + minute * 60_000).toISOString(),
  }))
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(Response.json({ items: completeWindow })))

  await renderView()

  expect(await screen.findByRole("img", { name: "Минутные свечи за последние два часа" })).toBeTruthy()
  expect(document.querySelectorAll(".candlestick-chart__wick")).toHaveLength(120)
  expect(screen.getByText("08:00")).toBeTruthy()
  expect(screen.getByText("09:59")).toBeTruthy()
})

it("prunes expired candles while retaining current cached candles after a failed periodic refresh", async () => {
  vi.setSystemTime("2026-08-05T10:00:00Z")
  const initial = [{ ...candles.items[0], started_at: "2026-08-05T08:00:00Z" }, ...candles.items]
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(Response.json({ items: initial }))
    .mockResolvedValueOnce(new Response(null, { status: 503 })))
  await renderView()
  await flushPromises()
  expect(document.querySelectorAll(".candlestick-chart__wick")).toHaveLength(2)

  await vi.advanceTimersByTimeAsync(60_000)
  await flushPromises()

  expect(document.querySelectorAll(".candlestick-chart__wick")).toHaveLength(1)
  expect(screen.getByText("09:59")).toBeTruthy()
  expect(screen.queryByText("08:00")).toBeNull()
  expect(screen.getByText("Не удалось обновить свечи.")).toBeTruthy()
})

it("keeps one candle request in flight and does not resume polling after unmount", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-08-05T10:00:00Z")
  let resolveCandles!: (response: Response) => void
  const pending = new Promise<Response>((resolve) => { resolveCandles = resolve })
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockReturnValueOnce(pending)
  vi.stubGlobal("fetch", fetchMock)
  await renderView()
  await flushPromises()
  await vi.advanceTimersByTimeAsync(120_000)
  expect(fetchMock).toHaveBeenCalledTimes(2)

  cleanup()
  resolveCandles(Response.json(candles))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it("creates trading automation for a user-selected account", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(details))
    .mockResolvedValueOnce(Response.json(candles))
    .mockResolvedValueOnce(Response.json({ accounts: [{ account_id: "account-1", name: "Sandbox account" }], errors: [], total_amounts: [], total_free_cash: [] }))
    .mockResolvedValueOnce(Response.json({ id: "auto-1", state: "IN_QUEUE" }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()

  await fireEvent.click(await screen.findByRole("button", { name: "Торговля" }))
  await fireEvent.update(await screen.findByLabelText("Счёт"), "account-1")
  await fireEvent.click(screen.getByRole("button", { name: "Создать автомат" }))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4))
  expect(fetchMock).toHaveBeenNthCalledWith(4, "/api/instruments/broker-1/row-1/trade", expect.objectContaining({ method: "POST" }))
})
