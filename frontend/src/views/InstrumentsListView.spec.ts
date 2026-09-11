import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, expect, it, vi } from "vitest"

import InstrumentsListView from "./InstrumentsListView.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const catalog = {
  broker_id: "broker-1",
  items: [{ id: "row-1", broker_id: "broker-1", instrument_id: "uid-1", figi: "figi-1", ticker: "SBER", name: "Sber", class_code: "TQBR", instrument_type: "INSTRUMENT_TYPE_SHARE", category: "SHARE", currency: "RUB", lot: 10, unit_price: "312.45", lot_price: "3124.50", price_captured_at: "2026-08-05T12:00:00Z", api_trade_available: true, is_active: true, is_selected: true, first_seen_at: "2026-08-05T10:00:00Z", last_seen_at: "2026-08-05T12:00:00Z" }],
  categories: [{ code: "SHARE", label: "Акции", count: 1 }],
  currencies: ["RUB", "USD"],
  sync_state: { broker_id: "broker-1", status: "SUCCESS", last_attempt_at: "2026-08-05T12:00:00Z", last_success_at: "2026-08-05T12:00:00Z", safe_error: null },
}

const catalogWithTwoItems = {
  ...catalog,
  items: [
    catalog.items[0],
    { ...catalog.items[0], id: "row-2", instrument_id: "uid-2", figi: "figi-2", ticker: "GAZP", name: "Gazprom", is_selected: false },
  ],
  categories: [{ code: "SHARE", label: "Акции", count: 2 }],
}

it("renders backend categories, highlights selected item and opens ItemView", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "Sandbox", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalog)))
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: { template: "<div>details</div>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  expect(await screen.findByRole("button", { name: "Акции 1" })).toBeTruthy()
  const row = screen.getByRole("row", { name: /SBER Sber Акции RUB 10 312.45 3124.50 Активен Исключить SBER из отбора/ })
  expect(row.classList.contains("data-table__row--marked")).toBe(true)
  await fireEvent.click(row)
  await fireEvent.click(screen.getByRole("button", { name: "Подробнее" }))

  await waitFor(() => expect(router.currentRoute.value.name).toBe("instrument-details"))
  expect(router.currentRoute.value.params).toMatchObject({ brokerId: "broker-1", instrumentId: "row-1" })
})

it("passes selected-only filter to backend", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "Sandbox", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json(catalog))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: { template: "<div/>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  await screen.findByRole("button", { name: "Только выделенные" })
  await fireEvent.click(screen.getByRole("button", { name: "Только выделенные" }))

  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/brokers/broker-1/instruments?include_inactive=false&selected_only=true&currency=RUB",
  ))
})

it("loads RUB by default and requests another currency after switching", async () => {
  const usdCatalog = {
    ...catalog,
    items: [{ ...catalog.items[0], ticker: "USD000UTSTOM", currency: "USD" }],
  }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "Sandbox", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json(usdCatalog))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: { template: "<div/>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  await screen.findByText("SBER")
  expect(fetchMock).toHaveBeenNthCalledWith(2,
    "/api/brokers/broker-1/instruments?include_inactive=false&selected_only=false&currency=RUB",
  )
  await fireEvent.update(screen.getByRole("combobox", { name: "Валюта инструментов" }), "USD")
  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/brokers/broker-1/instruments?include_inactive=false&selected_only=false&currency=USD",
  ))
  expect(await screen.findByText("USD000UTSTOM")).toBeTruthy()
})

it("shows unit and lot prices and applies the lot-price range on backend", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "Sandbox", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json(catalog))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: { template: "<div/>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  expect(await screen.findByText("312.45")).toBeTruthy()
  expect(screen.getByText("3124.50")).toBeTruthy()
  await fireEvent.update(screen.getByLabelText("Цена лота от"), "3000")
  await fireEvent.update(screen.getByLabelText("Цена лота до"), "4000")
  await fireEvent.click(screen.getByRole("button", { name: "Применить цену" }))

  await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/brokers/broker-1/instruments?include_inactive=false&selected_only=false&lot_price_from=3000&lot_price_to=4000&currency=RUB",
  ))
})

it("filters the current grid by ticker or name without another backend request", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "Sandbox", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalogWithTwoItems))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: { template: "<div/>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  await screen.findByText("SBER")
  const search = screen.getByRole("searchbox", { name: "Поиск по тикеру или названию" })
  await fireEvent.update(search, "gaz")
  expect(screen.queryByText("SBER")).toBeNull()
  expect(screen.getByText("GAZP")).toBeTruthy()
  await fireEvent.update(search, "sBeR")
  expect(screen.getByText("SBER")).toBeTruthy()
  expect(screen.queryByText("GAZP")).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it("resets all filters with one backend request", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "Sandbox", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json(catalog))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: { template: "<div/>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Акции 1" }))
  await fireEvent.click(screen.getByRole("button", { name: "Только выделенные" }))
  await fireEvent.update(screen.getByRole("searchbox"), "SBER")
  await fireEvent.update(screen.getByLabelText("Цена лота от"), "3000")
  await fireEvent.update(screen.getByLabelText("Цена лота до"), "4000")
  const callsBeforeReset = fetchMock.mock.calls.length
  await fireEvent.click(screen.getByRole("button", { name: "Сбросить фильтры" }))

  await waitFor(() => expect(fetchMock.mock.calls.length).toBe(callsBeforeReset + 1))
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/brokers/broker-1/instruments?include_inactive=false&selected_only=false&currency=RUB",
  )
  expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("")
  expect((screen.getByLabelText("Цена лота от") as HTMLInputElement).value).toBe("")
  expect((screen.getByLabelText("Цена лота до") as HTMLInputElement).value).toBe("")
})

it("uses an accessible boolean switch to update selection", async () => {
  const unselected = { ...catalog.items[0], is_selected: false }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "Sandbox", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json(unselected))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: { template: "<div/>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  const selection = await screen.findByRole("switch", { name: "Исключить SBER из отбора" })
  expect(selection.getAttribute("aria-checked")).toBe("true")
  await fireEvent.click(selection)
  await waitFor(() => expect(selection.getAttribute("aria-checked")).toBe("false"))
  expect(screen.getByRole("switch", { name: "Добавить SBER в отбор" })).toBeTruthy()
  expect(fetchMock).toHaveBeenNthCalledWith(
    3,
    "/api/brokers/broker-1/instruments/row-1/selection",
    expect.objectContaining({ method: "PUT" }),
  )
})
