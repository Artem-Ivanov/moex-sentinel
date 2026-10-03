import { cleanup, render, screen } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, expect, it, vi } from "vitest"

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
