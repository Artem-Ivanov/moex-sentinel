import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, expect, it, vi } from "vitest"

import BrokersView from "./BrokersView.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const broker = {
  id: "broker-1",
  display_name: "Sandbox",
  provider_code: "TINVEST",
  environment_code: "SANDBOX",
  adapter_code: "TINVEST_SANDBOX",
  enabled: true,
  fields: [
    { name: "token", value: "test-connection-value" },
    { name: "fqdn", value: "sandbox.example:443" },
  ],
  is_test: true,
  account_id: "account-1",
  created_at: "2026-08-11T10:00:00Z",
  updated_at: "2026-08-11T10:00:00Z",
}

async function renderView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/brokers", name: "brokers", component: BrokersView },
      { path: "/brokers/:brokerId/accounts", name: "broker-accounts", component: { template: "<div/>" } },
    ],
  })
  await router.push({ name: "brokers" })
  await router.isReady()
  return render(BrokersView, { global: { plugins: [router] } })
}

it("loads broker settings without a separate environment request", async () => {
  const fetchMock = vi.fn().mockResolvedValue(Response.json({ adapters: [], brokers: [broker] }))
  vi.stubGlobal("fetch", fetchMock)

  await renderView()

  expect(await screen.findByText("Sandbox")).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock).toHaveBeenCalledWith("/api/brokers")
  expect(screen.queryByRole("button", { name: "Заполнить тестовый контур" })).toBeNull()
})

it("locks a selected account even when the connection is disabled", async () => {
  const disabledBroker = { ...broker, enabled: false }
  const updated = { ...disabledBroker, display_name: "Updated" }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [disabledBroker] }))
    .mockResolvedValueOnce(Response.json(updated))
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [updated] }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()

  await fireEvent.dblClick((await screen.findByText("Sandbox")).closest("article") as HTMLElement)
  expect((screen.getByLabelText("Идентификатор брокерского счёта") as HTMLInputElement).readOnly).toBe(true)
  expect(screen.getByText("Счёт закреплён за подключением. Для другого счёта создайте новое подключение.")).toBeTruthy()
  await fireEvent.update(screen.getByLabelText("Идентификатор брокерского счёта"), "account-2")
  await fireEvent.update(screen.getByLabelText("Название"), "Updated")
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  const request = fetchMock.mock.calls[1][1] as RequestInit
  expect(JSON.parse(String(request.body))).toMatchObject({ account_id: "account-1", display_name: "Updated" })
})

it("cancels a failed edit without losing the selected broker or keeping its errors", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [broker] }))
    .mockResolvedValueOnce(Response.json({ detail: {
      code: "VALIDATION_ERROR",
      message: "Проверьте настройки брокера.",
      fields: [{ path: "display_name", code: "REQUIRED", message: "Укажите название." }],
    } }, { status: 422 }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()

  const row = (await screen.findByText("Sandbox")).closest("article") as HTMLElement
  await fireEvent.click(row)
  await fireEvent.dblClick(row)
  await fireEvent.update(screen.getByLabelText("Название"), "")
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }))

  expect(await screen.findByText("Укажите название.")).toBeTruthy()
  expect(screen.getByLabelText(/^Название/).getAttribute("aria-invalid")).toBe("true")
  await fireEvent.click(screen.getByRole("button", { name: "Отмена" }))

  expect(screen.queryByText("Проверьте настройки брокера.")).toBeNull()
  expect(screen.queryByText("Укажите название.")).toBeNull()
  expect(screen.queryByRole("heading", { name: "Редактирование интеграции" })).toBeNull()
  const restoredRow = screen.getByText("Sandbox").closest("article") as HTMLElement
  expect(restoredRow.className).toContain("data-table__row--selected")
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await fireEvent.dblClick(restoredRow)
  expect((screen.getByLabelText("Название") as HTMLInputElement).value).toBe("Sandbox")
  expect(screen.getByLabelText("Название").getAttribute("aria-invalid")).toBe("false")
})

it("closes the deleted broker's editor and keeps it absent after remount", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [broker] }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockImplementation(() => Promise.resolve(Response.json({ adapters: [], brokers: [] })))
  vi.stubGlobal("fetch", fetchMock)
  const view = await renderView()

  await fireEvent.dblClick((await screen.findByText("Sandbox")).closest("article") as HTMLElement)
  await fireEvent.click(screen.getByRole("button", { name: "Удалить" }))

  expect(await screen.findByText("Подключённые брокеры отсутствуют")).toBeTruthy()
  expect(screen.queryByRole("heading", { name: "Редактирование интеграции" })).toBeNull()
  expect(screen.queryByText("Sandbox")).toBeNull()
  expect(document.querySelector(".data-table__row--selected")).toBeNull()
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/brokers/broker-1", expect.objectContaining({ method: "DELETE" }))
  view.unmount()
  await renderView()
  expect(await screen.findByText("Подключённые брокеры отсутствуют")).toBeTruthy()
  expect(screen.queryByText("Sandbox")).toBeNull()
})

it("keeps an unrelated disabled broker's editor open after deletion", async () => {
  const disabledBroker = { ...broker, id: "broker-2", display_name: "Disabled", enabled: false }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [broker, disabledBroker] }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [disabledBroker] }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()

  await fireEvent.dblClick((await screen.findByText("Disabled")).closest("article") as HTMLElement)
  const deletedRow = screen.getByText("Sandbox").closest("article") as HTMLElement
  await fireEvent.click(within(deletedRow).getByRole("button", { name: "Удалить" }))

  await waitFor(() => expect(screen.queryByText("Sandbox")).toBeNull())
  expect(screen.getByText("Disabled")).toBeTruthy()
  expect(screen.getByRole("heading", { name: "Редактирование интеграции" })).toBeTruthy()
  expect((screen.getByLabelText("Включено") as HTMLInputElement).checked).toBe(false)
  expect((screen.getByLabelText("Идентификатор брокерского счёта") as HTMLInputElement).readOnly).toBe(true)
  await fireEvent.update(screen.getByLabelText("Название"), "Updated disabled")
  expect((screen.getByLabelText("Название") as HTMLInputElement).value).toBe("Updated disabled")
})

it.each(["backend", "network"])("shows a %s deletion failure and allows retry with the editor and selection preserved", async (failure) => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [broker] }))
  if (failure === "backend") {
    fetchMock.mockResolvedValueOnce(Response.json({ detail: {
      code: "BROKER_IN_USE",
      message: "Брокер используется торговым автоматом.",
      fields: [],
    } }, { status: 409 }))
  } else {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"))
  }
  fetchMock
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [] }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()

  const row = (await screen.findByText("Sandbox")).closest("article") as HTMLElement
  await fireEvent.dblClick(row)
  await fireEvent.click(screen.getByRole("button", { name: "Удалить" }))

  const message = failure === "backend"
    ? "Брокер используется торговым автоматом."
    : "Не удалось удалить брокера."
  expect(await screen.findByText(message)).toBeTruthy()
  expect(screen.getByText("Sandbox").closest("article")?.className).toContain("data-table__row--selected")
  expect(screen.getByRole("heading", { name: "Редактирование интеграции" })).toBeTruthy()
  expect((screen.getByLabelText("Идентификатор брокерского счёта") as HTMLInputElement).readOnly).toBe(true)
  await fireEvent.click(screen.getByRole("button", { name: "Удалить" }))

  expect(await screen.findByText("Подключённые брокеры отсутствуют")).toBeTruthy()
  expect(screen.queryByText(message)).toBeNull()
  expect(screen.queryByText("Sandbox")).toBeNull()
  expect(screen.queryByRole("heading", { name: "Редактирование интеграции" })).toBeNull()
  expect(document.querySelector(".data-table__row--selected")).toBeNull()
})

it("derives PROD identity and fixed endpoint from the selected server adapter", async () => {
  const adapter = { adapter_code: "TINVEST_PROD", provider_code: "TINVEST", environment_code: "PROD", fields: [
    { name: "token", required: true, default_value: null },
    { name: "fqdn", required: true, default_value: "invest-public-api.tinkoff.ru:443" },
  ] }
  const fetchMock = vi.fn().mockResolvedValue(Response.json({ adapters: [adapter], brokers: [] }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  await fireEvent.click(screen.getByRole("button", { name: "Добавить брокера" }))
  await screen.findByRole("option", { name: "TINVEST · PROD" })
  await fireEvent.update(screen.getByLabelText("Адаптер"), "TINVEST_PROD")
  const testFlag = screen.getByLabelText("Тестовое подключение") as HTMLInputElement
  expect(testFlag.checked).toBe(false)
  expect(testFlag.disabled).toBe(true)
  const endpoint = screen.getByLabelText("fqdn") as HTMLInputElement
  expect(endpoint.value).toBe("invest-public-api.tinkoff.ru:443")
  expect(endpoint.readOnly).toBe(true)
  expect((screen.getByLabelText("token") as HTMLInputElement).type).toBe("password")
})
