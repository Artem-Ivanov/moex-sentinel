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

it.each([{ brokers: [] }, { brokers: [{ id: "draft-1", display_name: "Sandbox", enabled: false }] }])(
  "guides users to broker settings when no enabled connection exists (%j)",
  async ({ brokers }) => {
    const fetchMock = vi.fn().mockResolvedValueOnce(Response.json({ adapters: [], brokers }))
    vi.stubGlobal("fetch", fetchMock)
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: "/instruments", name: "instruments", component: InstrumentsListView },
      { path: "/brokers", name: "brokers", component: { template: "<div>Настройки брокера</div>" } },
    ] })
    await router.push({ name: "instruments" }); await router.isReady()
    render(InstrumentsListView, { global: { plugins: [router] } })

    expect(await screen.findByText("Чтобы загрузить инструменты, настройте подключение брокера и выберите счёт.")).toBeTruthy()
    expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByRole("option", { name: "Sandbox" })).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await fireEvent.click(screen.getByRole("link", { name: "Настроить брокера" }))
    await waitFor(() => expect(router.currentRoute.value.name).toBe("brokers"))
  },
)

it("does not show broker setup guidance while settings load or fail", async () => {
  let rejectSettings!: (reason: Error) => void
  vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise((_resolve, reject) => { rejectSettings = reject })))
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/brokers", name: "brokers", component: { template: "<div/>" } },
  ] })
  await router.push({ name: "instruments" }); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })

  expect(screen.queryByRole("link", { name: "Настроить брокера" })).toBeNull()
  rejectSettings(new Error("Settings unavailable"))
  expect(await screen.findByText("Не удалось загрузить справочник инструментов.")).toBeTruthy()
  expect(screen.queryByRole("link", { name: "Настроить брокера" })).toBeNull()
})

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

it.each([false, true])("shows adoption exceptions despite catalog success or refresh failure (%s)", async (refreshFails) => {
  const account = "production-account-12345678"
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [{ id: "broker-1", display_name: "PROD", enabled: true }] }))
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(Response.json({ broker_id: "broker-1", added: 0, updated: 1, deactivated: 0, synchronized_at: "2026-10-02T00:00:00Z", adoption: {
      adopted: 2, existing: 3, held: 1, skipped: 2, diagnostics: [
        { account_id: account, external_instrument_id: "uid-blocked", reason: "BOOTSTRAP_BLOCKED_INVENTORY" },
        { account_id: account, external_instrument_id: "uid-short", reason: "BOOTSTRAP_INVALID_QUANTITY" },
        { account_id: account, external_instrument_id: "uid-order", reason: "BOOTSTRAP_ACTIVE_ORDER" },
        { account_id: account, external_instrument_id: "uid-conflict", reason: "BOOTSTRAP_CONFLICT" },
      ],
    } }))
    .mockResolvedValueOnce(refreshFails
      ? Response.json({ detail: { message: "Catalog refresh unavailable." } }, { status: 503 })
      : Response.json(catalog))
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/instruments", name: "instruments", component: InstrumentsListView }] })
  await router.push("/instruments"); await router.isReady()
  render(InstrumentsListView, { global: { plugins: [router] } })
  await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Обновить" }))
  expect(await screen.findByText("Позиции: принято 2, уже учтено 3, требуют сверки 1, пропущено 2.")).toBeTruthy()
  expect(screen.getByRole("alert").textContent).toContain("Заблокированный остаток")
  expect(screen.getByRole("alert").textContent).toContain("Короткая позиция или некорректное количество")
  expect(screen.getByRole("alert").textContent).toContain("Есть активная заявка")
  expect(screen.getByRole("alert").textContent).toContain("Новый снимок не принят")
  expect(screen.getByRole("alert").textContent).toContain("не подтверждает остановку автомата")
  expect(document.body.textContent).not.toContain("HOLD")
  expect(screen.getByRole("alert").textContent).toContain("••••5678")
  expect(document.body.textContent).not.toContain(account)
  expect(screen.getByText(/Добавлено: 0, обновлено: 1/)).toBeTruthy()
  if (refreshFails) expect(await screen.findByText("Catalog refresh unavailable.")).toBeTruthy()
})

function deferredResponse() {
  let resolve!: (value: Response) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function renderCatalogue(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/instruments", component: InstrumentsListView },
    { path: "/elsewhere", component: { template: "<div>Другая страница</div>" } },
  ] })
  await router.push("/instruments"); await router.isReady()
  render({ template: "<RouterView/>" }, { global: { plugins: [router] } })
  return router
}
const twoBrokerSettings = { adapters: [], brokers: [
  { id: "broker-1", display_name: "First", enabled: true },
  { id: "broker-2", display_name: "Second", enabled: true },
] }
const otherCatalog = { ...catalog, broker_id: "broker-2", items: [{ ...catalog.items[0], broker_id: "broker-2", ticker: "SECOND" }] }
const syncResult = { broker_id: "broker-1", added: 7, updated: 0, deactivated: 0, synchronized_at: "2026-10-04T00:00:00Z", adoption: { adopted: 7, existing: 0, held: 0, skipped: 0, diagnostics: [] } }
const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

it.each(["success", "error"])("keeps newest catalogue after an older filter %s", async (outcome) => {
  const older = deferredResponse(); const newer = deferredResponse()
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise))
  await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Акции 1" }))
  await fireEvent.click(screen.getByRole("button", { name: "Только выделенные" }))
  newer.resolve(Response.json({ ...catalog, items: [{ ...catalog.items[0], ticker: "NEWEST" }] }))
  await screen.findByText("NEWEST")
  if (outcome === "success") older.resolve(Response.json({ ...catalog, items: [{ ...catalog.items[0], ticker: "STALE" }] }))
  else older.reject(new Error("stale catalogue error"))
  await settle()
  expect(screen.queryByText("STALE")).toBeNull()
  expect(screen.queryByText("stale catalogue error")).toBeNull()
  expect(screen.getByText("NEWEST")).toBeTruthy()
})
it("keeps newer catalogue loading while an older read finishes", async () => {
  const older = deferredResponse(); const newer = deferredResponse()
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise))
  await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Акции 1" }))
  await fireEvent.click(screen.getByRole("button", { name: "Сбросить фильтры" }))
  older.resolve(Response.json(catalog)); await settle()
  expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(true)
  newer.resolve(Response.json(catalog))
  await waitFor(() => expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(false))
})
it("clears old broker actions and keeps broker filters interactive", async () => {
  const pending = deferredResponse()
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(pending.promise))
  await fireEvent.click((await screen.findByText("SBER")).closest("tr")!)
  await fireEvent.update(screen.getByRole("combobox", { name: "Брокер" }), "broker-2")
  expect(screen.queryByText("SBER")).toBeNull()
  expect((screen.getByRole("button", { name: "Подробнее" }) as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByRole("combobox", { name: "Брокер" }) as HTMLSelectElement).disabled).toBe(false)
  pending.resolve(Response.json(otherCatalog)); await screen.findByText("SECOND")
})
it("preserves selected row during same-scope refresh", async () => {
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockResolvedValueOnce(Response.json(catalog)))
  await fireEvent.click((await screen.findByText("SBER")).closest("tr")!)
  await fireEvent.click(screen.getByRole("button", { name: "Применить цену" }))
  await waitFor(() => expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByRole("button", { name: "Подробнее" }) as HTMLButtonElement).disabled).toBe(false)
})
it.each(["success", "error"])("ignores old sync %s after broker A to B to A", async (outcome) => {
  const pending = deferredResponse()
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(Response.json(otherCatalog)).mockResolvedValueOnce(Response.json(catalog))
  await renderCatalogue(fetchMock); await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Обновить" }))
  await fireEvent.update(screen.getByRole("combobox", { name: "Брокер" }), "broker-2"); await screen.findByText("SECOND")
  await fireEvent.update(screen.getByRole("combobox", { name: "Брокер" }), "broker-1"); await screen.findByText("SBER")
  if (outcome === "success") pending.resolve(Response.json(syncResult))
  else pending.reject(new Error("old sync error"))
  await settle()
  expect(screen.queryByText(/Добавлено: 7/)).toBeNull()
  expect(screen.queryByText("old sync error")).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(5)
})
it("ignores old selection failure in replacement catalogue", async () => {
  const pending = deferredResponse()
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(Response.json(catalog)))
  await fireEvent.click(await screen.findByRole("switch"))
  await fireEvent.click(screen.getByRole("button", { name: "Применить цену" }))
  await waitFor(() => expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(false))
  pending.reject(new Error("selection failed")); await settle()
  expect(screen.queryByText("Не удалось изменить отбор инструмента.")).toBeNull()
  expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true")
})
it.each(["settings", "sync"])("does not start catalogue read after unmount with pending %s", async (kind) => {
  const pending = deferredResponse()
  const fetchMock = kind === "settings" ? vi.fn().mockReturnValueOnce(pending.promise) : vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(pending.promise)
  const router = await renderCatalogue(fetchMock)
  if (kind === "sync") { await screen.findByText("SBER"); await fireEvent.click(screen.getByRole("button", { name: "Обновить" })) }
  await router.push("/elsewhere"); await screen.findByText("Другая страница")
  pending.resolve(Response.json(kind === "settings" ? twoBrokerSettings : syncResult)); await settle()
  expect(fetchMock).toHaveBeenCalledTimes(kind === "settings" ? 1 : 3)
})

it("keeps newest catalogue error after older success", async () => {
  const older = deferredResponse(); const newer = deferredResponse()
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise))
  await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Акции 1" }))
  await fireEvent.click(screen.getByRole("button", { name: "Сбросить фильтры" }))
  newer.reject(new Error("current catalogue error")); await screen.findByText("current catalogue error")
  older.resolve(Response.json({ ...catalog, items: [{ ...catalog.items[0], ticker: "STALE" }] })); await settle()
  expect(screen.getByText("current catalogue error")).toBeTruthy()
  expect(screen.queryByText("STALE")).toBeNull()
})
it("uses the requested filter snapshot while price edits remain drafts", async () => {
  const pending = deferredResponse()
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(pending.promise)
  await renderCatalogue(fetchMock); await screen.findByText("SBER")
  await fireEvent.update(screen.getByLabelText("Цена лота от"), "100")
  await fireEvent.update(screen.getByLabelText("Цена лота до"), "500")
  await fireEvent.update(screen.getByRole("combobox", { name: "Валюта инструментов" }), "USD")
  expect(fetchMock).toHaveBeenLastCalledWith("/api/brokers/broker-1/instruments?include_inactive=false&selected_only=false&lot_price_from=100&lot_price_to=500&currency=USD")
  await fireEvent.update(screen.getByLabelText("Цена лота от"), "200")
  pending.resolve(Response.json({ ...catalog, items: [{ ...catalog.items[0], ticker: "REQUESTED" }] }))
  await screen.findByText("REQUESTED")
  expect(fetchMock).toHaveBeenCalledTimes(3)
  expect((screen.getByLabelText("Цена лота от") as HTMLInputElement).value).toBe("200")
})
it.each(["filter-first", "sync-first"])("applies latest read across sync reload and filters (%s)", async (order) => {
  const sync = deferredResponse(); const first = deferredResponse(); const second = deferredResponse()
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(sync.promise).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  await renderCatalogue(fetchMock); await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Обновить" }))
  if (order === "filter-first") {
    await fireEvent.click(screen.getByRole("button", { name: "Только выделенные" }))
    sync.resolve(Response.json(syncResult)); await screen.findByText(/Добавлено: 7/)
  } else {
    sync.resolve(Response.json(syncResult)); await screen.findByText(/Добавлено: 7/)
    await fireEvent.click(screen.getByRole("button", { name: "Только выделенные" }))
  }
  first.resolve(Response.json({ ...catalog, items: [{ ...catalog.items[0], ticker: "STALE" }] })); await settle()
  expect(screen.queryByText("STALE")).toBeNull()
  expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(true)
  second.resolve(Response.json({ ...catalog, items: [{ ...catalog.items[0], ticker: "LATEST" }] }))
  await screen.findByText("LATEST")
  await waitFor(() => expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(false))
  expect(screen.getByText(/Позиции: принято 7/)).toBeTruthy()
})
it("does not apply old selection success to a new object with the same id", async () => {
  const pending = deferredResponse()
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(Response.json(catalog)))
  await fireEvent.click(await screen.findByRole("switch"))
  await fireEvent.click(screen.getByRole("button", { name: "Применить цену" }))
  await waitFor(() => expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(false))
  pending.resolve(Response.json({ ...catalog.items[0], is_selected: false })); await settle()
  expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true")
})
it("clears row selection when a refreshed catalogue removes the selected row", async () => {
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockResolvedValueOnce(Response.json({ ...catalog, items: [] })))
  await fireEvent.click((await screen.findByText("SBER")).closest("tr")!)
  await fireEvent.click(screen.getByRole("button", { name: "Применить цену" }))
  await screen.findByText("Справочник инструментов пуст")
  expect((screen.getByRole("button", { name: "Подробнее" }) as HTMLButtonElement).disabled).toBe(true)
})

it("releases sync busy after mutation while the newest read owns loading", async () => {
  const sync = deferredResponse(); const syncRead = deferredResponse(); const latestRead = deferredResponse()
  await renderCatalogue(vi.fn().mockResolvedValueOnce(Response.json(twoBrokerSettings)).mockResolvedValueOnce(Response.json(catalog)).mockReturnValueOnce(sync.promise).mockReturnValueOnce(syncRead.promise).mockReturnValueOnce(latestRead.promise))
  await screen.findByText("SBER")
  await fireEvent.click(screen.getByRole("button", { name: "Обновить" }))
  sync.resolve(Response.json(syncResult)); await screen.findByText(/Добавлено: 7/)
  await fireEvent.click(screen.getByRole("button", { name: "Только выделенные" }))
  latestRead.resolve(Response.json({ ...catalog, items: [{ ...catalog.items[0], ticker: "LATEST" }] }))
  await screen.findByText("LATEST")
  expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(false)
  syncRead.resolve(Response.json(catalog)); await settle()
  expect(screen.getByText("LATEST")).toBeTruthy()
})
