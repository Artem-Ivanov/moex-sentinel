import { cleanup, render, screen } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, describe, expect, it } from "vitest"

import App from "./App.vue"

const EmptyPage = { template: "<div>page</div>" }

afterEach(cleanup)

async function renderAt(routeName: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/brokers", name: "brokers", component: EmptyPage },
      { path: "/instruments", name: "instruments", component: EmptyPage },
      { path: "/positions", name: "positions", component: EmptyPage },
    ],
  })
  await router.push({ name: routeName })
  await router.isReady()
  render(App, { global: { plugins: [router], stubs: { HealthStatus: true, EnvironmentSwitch: true, TradingSessionStatus: true } } })
}

describe("sidebar navigation", () => {
  it("uses page routes without the backend API prefix", async () => {
    await renderAt("positions")

    expect(screen.queryByRole("link", { name: "Аналитика" })).toBeNull()
    expect(screen.getByRole("link", { name: "Брокеры" }).getAttribute("href")).toBe("/brokers")
    expect(screen.getByRole("link", { name: "Инструменты" }).getAttribute("href")).toBe(
      "/instruments",
    )
    expect(screen.getByRole("link", { name: "Торговля" }).getAttribute("href")).toBe(
      "/positions",
    )
    expect(screen.queryByRole("link", { name: "Стратегии" })).toBeNull()
    expect(screen.queryByRole("link", { name: "Последние операции" })).toBeNull()
  })

  it("marks only the current page as active", async () => {
    await renderAt("positions")

    expect(screen.getByRole("link", { name: "Торговля" }).classList).toContain(
      "nav-item--active",
    )
    expect(screen.queryByRole("link", { name: "Аналитика" })).toBeNull()
  })
})
