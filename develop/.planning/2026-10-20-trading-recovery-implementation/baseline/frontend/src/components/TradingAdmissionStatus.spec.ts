import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
import { afterEach, expect, it, vi } from "vitest"
import { currentSession } from "../auth"
import TradingAdmissionStatus from "./TradingAdmissionStatus.vue"

const brokerId = "11111111-1111-4111-8111-111111111111"
const runtimeId = "22222222-2222-4222-8222-222222222222"
const limits = {
  currency: "RUB", exposure_scope: "ACCOUNT_GROSS", max_order_notional: "1000",
  max_exposure: "5000", max_outstanding_orders: 2, max_daily_loss: "500",
}
const admission = {
  broker_id: brokerId,
  account_id: "account-1",
  revision: 3,
  owner_runtime_instance_id: runtimeId,
  owner_generation: 2,
  recovery_runtime_instance_id: null,
  admission_status: "STOP_REQUIRED",
  arm_revision: null,
  lease_expires_at: "2026-10-14T10:00:30.000Z",
  arm_expires_at: null,
  limits,
  configuration_fingerprint: null,
  baseline: null,
  reconciliation: null,
  effective_can_submit: true,
  effective_denial_reasons: [],
  unresolved_reservation_count: 0,
  outstanding_order_count: 0,
  recovery_only: true,
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  currentSession.value = null
})

it("edits four account-wide caps through the operator endpoint without manual admission steps", async () => {
  vi.useFakeTimers({ toFake: ["Date"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  currentSession.value = { username: "operator", csrf_token: "csrf-token" }
  const updated = { ...admission, revision: 4, limits: { ...limits, max_order_notional: "1250" } }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(admission))
    .mockResolvedValueOnce(Response.json(updated))
  vi.stubGlobal("fetch", fetchMock)
  render(TradingAdmissionStatus, { props: { brokerId, accountId: "account-1", positionState: "HOLD", resumeRequested: true } })

  const orderNotional = await screen.findByLabelText("Максимальный размер заявки, RUB")
  await waitFor(() => expect((orderNotional as HTMLInputElement).value).toBe("1000"))
  expect((screen.getByLabelText("Максимальная экспозиция счёта, RUB") as HTMLInputElement).value).toBe("5000")
  expect((screen.getByLabelText("Максимум незавершённых заявок") as HTMLInputElement).value).toBe("2")
  expect((screen.getByLabelText("Максимальный дневной убыток, RUB") as HTMLInputElement).value).toBe("500")
  expect(screen.getByText("HOLD")).toBeTruthy()
  expect(screen.getByText("Технические проверки Core сейчас пройдены; доступность конкретной заявки проверяется при её отправке.")).toBeTruthy()
  expect(screen.getByText(/Их увеличение может снять блокер у уже активных автоматов IN_WORK/)).toBeTruthy()
  expect(screen.queryByText(/ARM|canary|сверк/i)).toBeNull()

  await fireEvent.update(screen.getByLabelText("Максимальный размер заявки, RUB"), "1250")
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить лимиты счёта" }))
  await screen.findByText("Лимиты счёта сохранены в Core.")
  const [url, init] = fetchMock.mock.calls[1]
  expect(url).toBe(`/api/brokers/${brokerId}/trading-admission/limits`)
  expect(init.method).toBe("PUT")
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf-token")
  expect(init.body).toBe(JSON.stringify({ expected_revision: 3, limits: { ...limits, max_order_notional: "1250" } }))
})

it("keeps UNKNOWN fail-closed and refuses to save malformed caps", async () => {
  vi.useFakeTimers({ toFake: ["Date"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  const unknown = { ...admission, lease_expires_at: "2026-10-14T10:00:30.000Z", owner_runtime_instance_id: null, owner_generation: null, effective_can_submit: false, effective_denial_reasons: ["DATA_UNKNOWN"], limits: null }
  const fetchMock = vi.fn().mockResolvedValue(Response.json(unknown))
  vi.stubGlobal("fetch", fetchMock)
  render(TradingAdmissionStatus, { props: { brokerId, accountId: "account-1", positionState: "UNKNOWN", resumeRequested: false } })

  expect(await screen.findByText(/DATA_UNKNOWN:/)).toBeTruthy()
  expect(screen.getByText("Текущий владелец и generation не подтверждены.")).toBeTruthy()
  await fireEvent.update(screen.getByLabelText("Максимальный размер заявки, RUB"), "Infinity")
  expect(screen.getByRole("button", { name: "Сохранить лимиты счёта" }).hasAttribute("disabled")).toBe(true)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it("keeps the draft on its original revision after 409 until the user loads current caps", async () => {
  vi.useFakeTimers({ toFake: ["Date"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  currentSession.value = { username: "operator", csrf_token: "csrf-token" }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(admission))
    .mockResolvedValueOnce(new Response(null, { status: 409 }))
    .mockResolvedValueOnce(Response.json({ ...admission, revision: 4 }))
    .mockResolvedValueOnce(Response.json({ ...admission, revision: 5, limits: { ...limits, max_order_notional: "1350" } }))
  vi.stubGlobal("fetch", fetchMock)
  render(TradingAdmissionStatus, { props: { brokerId, accountId: "account-1", positionState: "IN_WORK", resumeRequested: false } })
  const orderNotional = await screen.findByLabelText("Максимальный размер заявки, RUB")
  await waitFor(() => expect((orderNotional as HTMLInputElement).value).toBe("1000"))
  await fireEvent.update(orderNotional, "1250")
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить лимиты счёта" }))

  expect(await screen.findByText("Лимиты изменились в Core. Обновляю статус; проверьте форму и повторите сохранение.")).toBeTruthy()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  expect(await screen.findByText(/revision 3; актуальная revision 4/)).toBeTruthy()
  expect((orderNotional as HTMLInputElement).value).toBe("1250")
  expect(screen.getByRole("button", { name: "Сохранить лимиты счёта" }).hasAttribute("disabled")).toBe(true)
  expect(fetchMock.mock.calls[1][1].body).toContain('"expected_revision":3')

  await fireEvent.click(screen.getByRole("button", { name: "Загрузить актуальные лимиты" }))
  expect((orderNotional as HTMLInputElement).value).toBe("1000")
  await fireEvent.update(orderNotional, "1350")
  await fireEvent.click(screen.getByRole("button", { name: "Сохранить лимиты счёта" }))
  await screen.findByText("Лимиты счёта сохранены в Core.")
  expect(fetchMock.mock.calls[3][1].body).toContain('"expected_revision":4')
})

it("does not rebase a dirty form when polling observes an external limits revision", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  const current = { ...admission, revision: 4, limits: { ...limits, max_order_notional: "2000" } }
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(admission))
    .mockResolvedValueOnce(Response.json(current))
    .mockReturnValue(new Promise<Response>(() => undefined))
  vi.stubGlobal("fetch", fetchMock)
  render(TradingAdmissionStatus, { props: { brokerId, accountId: "account-1", positionState: "IN_WORK", resumeRequested: false } })
  const orderNotional = await screen.findByLabelText("Максимальный размер заявки, RUB")
  await waitFor(() => expect((orderNotional as HTMLInputElement).value).toBe("1000"))
  await fireEvent.update(orderNotional, "1250")

  await vi.advanceTimersByTimeAsync(15_000)
  expect(await screen.findByText(/revision 3; актуальная revision 4/)).toBeTruthy()
  expect((orderNotional as HTMLInputElement).value).toBe("1250")
  expect(screen.getByRole("button", { name: "Сохранить лимиты счёта" }).hasAttribute("disabled")).toBe(true)
  expect(fetchMock).toHaveBeenCalledTimes(2)

  await fireEvent.click(screen.getByRole("button", { name: "Загрузить актуальные лимиты" }))
  expect((orderNotional as HTMLInputElement).value).toBe("2000")
})

it("does not send account edits when a stale view is being refreshed", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] })
  vi.setSystemTime("2026-10-14T09:59:45.000Z")
  const never = new Promise<Response>(() => undefined)
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(admission)).mockReturnValue(never)
  vi.stubGlobal("fetch", fetchMock)
  render(TradingAdmissionStatus, { props: { brokerId, accountId: "account-1", positionState: "IN_WORK", resumeRequested: false } })
  const orderNotional = await screen.findByLabelText("Максимальный размер заявки, RUB")
  await waitFor(() => expect((orderNotional as HTMLInputElement).value).toBe("1000"))
  await vi.advanceTimersByTimeAsync(15_000)
  expect(screen.getByRole("button", { name: "Сохранить лимиты счёта" }).hasAttribute("disabled")).toBe(true)
})
