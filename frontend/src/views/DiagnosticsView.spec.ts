import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/vue"
import { afterEach, expect, it, vi } from "vitest"
import { clearSession, sessionChecked } from "../auth"
import DiagnosticsView from "./DiagnosticsView.vue"

const snapshot = {
  captured_at: "2026-10-03T09:00:00.123Z", status: "UNKNOWN",
  core: { status: "OK", version: "0.1.0", database: "ok", schema: "compatible", reason: "READY" },
  runtime: { environment: "TEST", access_mode: "READ_ONLY" },
  awaiting_observations: ["worker", "broker", "analytics", "market", "outbox", "portfolio", "strategy"],
}

function deferred() {
  let resolve!: (response: Response) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

afterEach(() => { cleanup(); clearSession(); sessionChecked.value = false; vi.unstubAllGlobals(); vi.useRealTimers() })

it("loads once and distinguishes Core readiness from unknown trading observations", async () => {
  const pending = deferred()
  const fetchMock = vi.fn().mockReturnValue(pending.promise)
  vi.stubGlobal("fetch", fetchMock)
  render(DiagnosticsView)
  expect((await screen.findByRole("status")).textContent).toContain("Загрузка")
  expect(screen.getByText("Версия интерфейса: 0.2.0")).toBeTruthy()
  expect((screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement).disabled).toBe(true)
  pending.resolve(Response.json(snapshot))
  await screen.findByText("Общий статус: UNKNOWN")
  expect(screen.getByText("Core: OK")).toBeTruthy()
  expect(screen.getByText("База данных: ok")).toBeTruthy()
  expect(screen.getByText("Схема: compatible")).toBeTruthy()
  expect(screen.getByText("Причина: READY")).toBeTruthy()
  expect(screen.getByText("Версия Core: 0.1.0")).toBeTruthy()
  expect(screen.getByText("Версия Worker: UNKNOWN")).toBeTruthy()
  expect(screen.getByText("Версия Analytics: UNKNOWN")).toBeTruthy()
  expect(screen.getByText("2026-10-03T09:00:00.123Z").getAttribute("datetime")).toBe(snapshot.captured_at)
  expect(screen.getByText(/Серверное время снимка \(UTC\)/)).toBeTruthy()
  expect(screen.getByText("Среда: TEST")).toBeTruthy()
  expect(screen.getByText("Режим доступа Core: READ_ONLY")).toBeTruthy()
  expect(screen.getByText(/Режим доступа не подтверждает фактический режим стратегии/)).toBeTruthy()
  expect(screen.getAllByRole("listitem")).toHaveLength(7)
  for (const name of ["Worker", "Брокер", "Analytics", "Рынок", "Outbox", "Портфель", "Стратегия"]) {
    expect(screen.getByText(`${name}: UNKNOWN`)).toBeTruthy()
  }
  expect(screen.getByText(/UNKNOWN означает отсутствие наблюдений/)).toBeTruthy()
  vi.useFakeTimers()
  await vi.advanceTimersByTimeAsync(120000)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it("shows the observed Worker and Analytics versions independently", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
    ...snapshot,
    service_versions: {
      worker: { observation: "OBSERVED", version: "1.4.2", received_at: "2026-10-03T09:00:00.000Z", age_ms: 123, reason: "OBSERVED" },
      analytics: { observation: "OBSERVED", version: "2.3.1", received_at: "2026-10-03T08:59:59.000Z", age_ms: 1123, reason: "OBSERVED" },
    },
  })))
  render(DiagnosticsView)
  await screen.findByText("Версия Worker: 1.4.2")
  expect(screen.getByText("Версия Analytics: 2.3.1")).toBeTruthy()
  expect(screen.getByText("Версия Core: 0.1.0")).toBeTruthy()
})

it("does not display an UNKNOWN observation version as current", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
    ...snapshot,
    service_versions: {
      worker: { observation: "UNKNOWN", version: "9.8.7", received_at: "2026-10-03T08:00:00.000Z", age_ms: 3600000, reason: "STALE" },
      analytics: { observation: "UNKNOWN", version: null, received_at: null, age_ms: null, reason: "UNAVAILABLE" },
    },
  })))
  render(DiagnosticsView)
  await screen.findByText("Версия Worker: UNKNOWN")
  expect(screen.getByText("Версия Analytics: UNKNOWN")).toBeTruthy()
  expect(screen.queryByText("Версия Worker: 9.8.7")).toBeNull()
})

it("shows its own version during a diagnostics API failure", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private upstream detail", { status: 503 })))
  render(DiagnosticsView)
  expect(screen.getByText("Версия интерфейса: 0.2.0")).toBeTruthy()
  await screen.findByRole("alert")
  expect(screen.queryByText(/private upstream detail/)).toBeNull()
})

it.each([
  { status: "DOWN", core: { status: "DOWN", version: "0.1.0", database: "error", schema: "unknown", reason: "DATABASE_UNAVAILABLE" } },
  { status: "DEGRADED", core: { status: "DEGRADED", version: "0.1.0", database: "ok", schema: "not_ready", reason: "SCHEMA_NOT_READY" } },
])("shows an HTTP200 diagnostic failure: $status", async ({ status, core }) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...snapshot, status, core })))
  render(DiagnosticsView)
  await screen.findByText(`Общий статус: ${status}`)
  expect(screen.getByText(`Core: ${core.status}`)).toBeTruthy()
  expect(screen.getByText(`Схема: ${core.schema}`)).toBeTruthy()
  expect(screen.getByText(`Причина: ${core.reason}`)).toBeTruthy()
  expect(screen.queryByRole("alert")).toBeNull()
})

it("clears the old snapshot during refresh and failure, then permits manual retry", async () => {
  const pending = deferred()
  const fetchMock = vi.fn().mockResolvedValueOnce(Response.json(snapshot)).mockReturnValueOnce(pending.promise)
    .mockResolvedValueOnce(Response.json({ ...snapshot, captured_at: "2026-10-03T09:01:00.000Z" }))
  vi.stubGlobal("fetch", fetchMock)
  render(DiagnosticsView)
  await screen.findByText("Core: OK")
  const button = screen.getByRole("button", { name: "Обновить" }) as HTMLButtonElement
  await fireEvent.click(button)
  expect(screen.queryByText("Core: OK")).toBeNull()
  expect(screen.queryByText(snapshot.captured_at)).toBeNull()
  expect(button.disabled).toBe(true)
  pending.reject(new Error("private connection string"))
  expect((await screen.findByRole("alert")).textContent).toContain("Не удалось загрузить диагностику")
  expect(screen.queryByText(/private connection string/)).toBeNull()
  expect(screen.queryByText("Core: OK")).toBeNull()
  expect(button.disabled).toBe(false)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await fireEvent.click(button)
  await screen.findByText("2026-10-03T09:01:00.000Z")
  expect(screen.queryByRole("alert")).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(3)
})

it.each([401, 503])("shows a safe error for HTTP %s without automatic retries", async (status) => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("secret body", { status }))
  vi.stubGlobal("fetch", fetchMock)
  render(DiagnosticsView)
  await screen.findByRole("alert")
  expect(screen.queryByText(/secret body/)).toBeNull()
  expect(screen.queryByText("Core: OK")).toBeNull()
  vi.useFakeTimers()
  await vi.advanceTimersByTimeAsync(120000)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it.each(["resolve", "reject"] as const)("aborts on unmount and ignores a late %s", async (completion) => {
  const pending = deferred()
  let signal: AbortSignal | undefined
  vi.stubGlobal("fetch", vi.fn((_url, init: RequestInit) => { signal = init.signal as AbortSignal; return pending.promise }))
  const view = render(DiagnosticsView)
  await waitFor(() => expect(signal).toBeTruthy())
  view.unmount()
  expect(signal?.aborted).toBe(true)
  if (completion === "resolve") pending.resolve(Response.json(snapshot))
  else pending.reject(new Error("late private failure"))
  await Promise.resolve()
  await Promise.resolve()
  expect(screen.queryByText("Core: OK")).toBeNull()
  expect(screen.queryByRole("alert")).toBeNull()
})
