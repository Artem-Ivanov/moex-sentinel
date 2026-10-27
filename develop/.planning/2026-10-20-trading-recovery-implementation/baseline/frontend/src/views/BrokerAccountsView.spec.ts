import { cleanup, render, screen } from "@testing-library/vue"
import { createMemoryHistory, createRouter, RouterView } from "vue-router"
import { afterEach, expect, it, vi } from "vitest"
import { flushPromises } from "@vue/test-utils"

import BrokerAccountsView from "./BrokerAccountsView.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

async function renderAccounts(accounts: unknown[], errors: unknown[]) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ accounts, errors, total_amounts: [], total_free_cash: [] })))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/brokers/:brokerId/accounts", component: BrokerAccountsView }] })
  await router.push("/brokers/synthetic/accounts")
  await router.isReady()
  render(BrokerAccountsView, { global: { plugins: [router] } })
}

it("shows a broker read error without claiming accounts are absent", async () => {
  await renderAccounts([], [{ broker_name: "Test broker", account_id: null, code: "BROKER_ACCOUNT_NOT_FOUND", message: "Выбранный счёт недоступен." }])
  expect(await screen.findByText("Test broker: Выбранный счёт недоступен.")).toBeTruthy()
  expect(screen.queryByText("Счета отсутствуют")).toBeNull()
})

it("shows empty accounts when the read succeeded without errors", async () => {
  await renderAccounts([], [])
  expect(await screen.findByText("Счета отсутствуют")).toBeTruthy()
})

it("continues to display successfully loaded account balances", async () => {
  await renderAccounts([{ account_id: "synthetic", name: "Test account", broker_name: "Test broker", status: "OPEN", total_amount: { amount: "100", currency: "RUB" }, free_cash: { amount: "25", currency: "RUB" } }], [])
  expect(await screen.findByText("Test account")).toBeTruthy()
  expect(screen.getByText("Стоимость: 100 RUB · Свободно: 25 RUB")).toBeTruthy()
  expect(screen.queryByText("Счета отсутствуют")).toBeNull()
})


function deferredAccounts() {
  let resolve!: (response: Response) => void
  let reject!: (error: Error) => void
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const accountsA = { accounts: [{ account_id: "account-A", name: "Account A", broker_name: "Broker A", status: "OPEN", total_amount: { amount: "100", currency: "RUB" }, free_cash: null }], errors: [], total_amounts: [], total_free_cash: [] }
const accountsB = { accounts: [{ ...accountsA.accounts[0], account_id: "account-B", name: "Account B", broker_name: "Broker B" }], errors: [], total_amounts: [], total_free_cash: [] }

async function renderRoutedAccounts() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/brokers/:brokerId/accounts", component: BrokerAccountsView },
  ] })
  await router.push("/brokers/broker-A/accounts")
  await router.isReady()
  const view = render(RouterView, { global: { plugins: [router] } })
  await flushPromises()
  return { router, ...view }
}

it.each(["success", "error"])("loads the new broker while the old account request is pending and ignores late %s", async (outcome) => {
  const old = deferredAccounts()
  const next = deferredAccounts()
  const fetchMock = vi.fn((url: string) => url.includes("broker-B") ? next.promise : old.promise)
  vi.stubGlobal("fetch", fetchMock)
  const { router } = await renderRoutedAccounts()
  await router.push("/brokers/broker-B/accounts")
  await flushPromises()
  expect(fetchMock).toHaveBeenCalledWith("/api/brokers/broker-B/accounts")
  if (outcome === "success") old.resolve(Response.json(accountsA))
  else old.reject(new Error("Old accounts unavailable"))
  await flushPromises()
  expect(screen.queryByText("Account A")).toBeNull()
  expect(screen.queryByText("Не удалось загрузить счета.")).toBeNull()
  expect(screen.getByText("Загрузка…")).toBeTruthy()
  next.resolve(Response.json(accountsB))
  await flushPromises()
  expect(screen.getByText("Account B")).toBeTruthy()
})

it.each(["success", "error"])("retains new broker accounts after the old response %s", async (outcome) => {
  const old = deferredAccounts()
  vi.stubGlobal("fetch", vi.fn((url: string) => url.includes("broker-B") ? Promise.resolve(Response.json(accountsB)) : old.promise))
  const { router } = await renderRoutedAccounts()
  await router.push("/brokers/broker-B/accounts")
  await flushPromises()
  expect(screen.getByText("Account B")).toBeTruthy()
  if (outcome === "success") old.resolve(Response.json(accountsA))
  else old.reject(new Error("Old accounts unavailable"))
  await flushPromises()
  expect(screen.getByText("Account B")).toBeTruthy()
  expect(screen.queryByText("Account A")).toBeNull()
  expect(screen.queryByText("Не удалось загрузить счета.")).toBeNull()
})

it("clears old balances when switching to a broker whose account read fails", async () => {
  const next = deferredAccounts()
  vi.stubGlobal("fetch", vi.fn((url: string) => url.includes("broker-B") ? next.promise : Promise.resolve(Response.json(accountsA))))
  const { router } = await renderRoutedAccounts()
  expect(screen.getByText("Account A")).toBeTruthy()
  await router.push("/brokers/broker-B/accounts")
  await flushPromises()
  expect(screen.queryByText("Account A")).toBeNull()
  expect(screen.getByText("Загрузка…")).toBeTruthy()
  next.reject(new Error("New accounts unavailable"))
  await flushPromises()
  expect(screen.getByText("Не удалось загрузить счета.")).toBeTruthy()
  expect(screen.queryByText("Счета отсутствуют")).toBeNull()
})
