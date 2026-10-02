import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, describe, expect, it, vi } from "vitest"

import App from "./App.vue"
import { clearSession, currentSession, sessionChecked } from "./auth"

const EmptyPage = { template: "<div>page</div>" }

afterEach(() => { cleanup(); clearSession(); sessionChecked.value = false; vi.unstubAllGlobals() })

async function renderAt(routeName: string, authenticated = true) {
  if (!vi.isMockFunction(globalThis.fetch)) vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ environment: "TEST", access_mode: "READ_ONLY" }))))
  currentSession.value = authenticated ? { username: "operator", csrf_token: "csrf" } : null
  sessionChecked.value = authenticated
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/brokers", name: "brokers", component: EmptyPage },
      { path: "/login", name: "login", component: EmptyPage },
      { path: "/instruments", name: "instruments", component: EmptyPage },
      { path: "/positions", name: "positions", component: EmptyPage },
    ],
  })
  await router.push({ name: routeName })
  await router.isReady()
  render(App, { global: { plugins: [router], stubs: { HealthStatus: true, EnvironmentSwitch: true, TradingSessionStatus: true } } })
  return router
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

  it("logs out from the navigation and opens the login route", async () => {
    currentSession.value = { username: "operator", csrf_token: "csrf" }
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal("fetch", fetchMock)
    const router = await renderAt("positions")

    await fireEvent.click(screen.getByRole("button", { name: "Выйти" }))

    expect(fetchMock).toHaveBeenCalledWith("/api/auth/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": "csrf" },
    })
    await waitFor(() => expect(router.currentRoute.value.name).toBe("login"))
    expect(screen.queryByRole("navigation", { name: "Основная навигация" })).toBeNull()
  })

  it("does not render the application shell while session restoration is pending", async () => {
    await renderAt("positions", false)

    expect(screen.getByRole("main", { name: "Проверка сессии" }).getAttribute("aria-busy")).toBe("true")
    expect(screen.queryByRole("navigation", { name: "Основная навигация" })).toBeNull()
    expect(screen.queryByText("Локальная платформа")).toBeNull()
  })

  it("keeps the session and displays a retry error when logout fails", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 503 }))
    vi.stubGlobal("fetch", fetchMock)
    const router = await renderAt("positions")

    await fireEvent.click(screen.getByRole("button", { name: "Выйти" }))

    expect(currentSession.value?.username).toBe("operator")
    expect(router.currentRoute.value.name).toBe("positions")
    expect((await screen.findByRole("alert")).textContent).toContain("Не удалось завершить сеанс")
  })
})
