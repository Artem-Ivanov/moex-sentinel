import { cleanup, fireEvent, render, screen } from "@testing-library/vue"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, expect, it, vi } from "vitest"

import { clearSession, sessionChecked } from "../auth"
import LoginView from "./LoginView.vue"

afterEach(() => {
  cleanup()
  clearSession()
  sessionChecked.value = false
  vi.unstubAllGlobals()
})

it("clears the password after a rejected login response", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", name: "login", component: LoginView },
      { path: "/positions", name: "positions", component: { template: "<div>positions</div>" } },
    ],
  })
  await router.push("/login")
  render(LoginView, { global: { plugins: [router] } })

  await fireEvent.update(screen.getByLabelText("Имя пользователя"), "operator")
  const password = screen.getByLabelText("Пароль")
  await fireEvent.update(password, "wrong password")
  await fireEvent.click(screen.getByRole("button", { name: "Войти" }))

  expect((await screen.findByRole("alert")).textContent).toContain("Не удалось войти")
  expect((password as HTMLInputElement).value).toBe("")
})

it("asks the operator to retry later after a rate-limit response", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 429 })))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", name: "login", component: LoginView },
      { path: "/positions", name: "positions", component: { template: "<div>positions</div>" } },
    ],
  })
  await router.push("/login")
  render(LoginView, { global: { plugins: [router] } })
  await fireEvent.update(screen.getByLabelText("Имя пользователя"), "operator")
  await fireEvent.update(screen.getByLabelText("Пароль"), "password")
  await fireEvent.click(screen.getByRole("button", { name: "Войти" }))

  expect((await screen.findByRole("alert")).textContent).toContain("Попробуйте позже")
})
