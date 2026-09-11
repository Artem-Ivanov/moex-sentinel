import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
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

it("edits account identity through the normal broker resource", async () => {
  const updated = { ...broker, account_id: "account-2" }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [broker] }))
    .mockResolvedValueOnce(Response.json(updated))
    .mockResolvedValueOnce(Response.json({ adapters: [], brokers: [updated] }))
  vi.stubGlobal("fetch", fetchMock)
  await renderView()

  await fireEvent.dblClick((await screen.findByText("Sandbox")).closest("article") as HTMLElement)
  await fireEvent.update(screen.getByLabelText("Идентификатор брокерского счёта"), "account-2")
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }))

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  const request = fetchMock.mock.calls[1][1] as RequestInit
  expect(JSON.parse(String(request.body))).toMatchObject({ account_id: "account-2" })
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

it.each(["backend", "network"])("shows a %s deletion failure and allows retry with the selection preserved", async (failure) => {
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
  await fireEvent.click(row)
  await fireEvent.click(screen.getByRole("button", { name: "Удалить" }))

  const message = failure === "backend"
    ? "Брокер используется торговым автоматом."
    : "Не удалось удалить брокера."
  expect(await screen.findByText(message)).toBeTruthy()
  expect(screen.getByText("Sandbox").closest("article")?.className).toContain("data-table__row--selected")
  await fireEvent.click(screen.getByRole("button", { name: "Удалить" }))

  expect(await screen.findByText("Подключённые брокеры отсутствуют")).toBeTruthy()
  expect(screen.queryByText(message)).toBeNull()
  expect(screen.queryByText("Sandbox")).toBeNull()
  expect(document.querySelector(".data-table__row--selected")).toBeNull()
})
