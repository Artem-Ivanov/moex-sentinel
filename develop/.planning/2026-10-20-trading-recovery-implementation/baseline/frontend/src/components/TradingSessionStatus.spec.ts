import { cleanup, render, screen } from "@testing-library/vue"
import { flushPromises } from "@vue/test-utils"
import { afterEach, expect, it, vi } from "vitest"

import TradingSessionStatus from "./TradingSessionStatus.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

it("shows aggregated live trading session status", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
    status: "OPEN", total: 2, open: 1, closed: 1, unavailable: 0,
  })))

  render(TradingSessionStatus)

  expect(await screen.findByText("Торги доступны: 1/2")).toBeTruthy()
})

it("does not start polling after unmount while the initial request is pending", async () => {
  vi.useFakeTimers()
  let resolveRequest!: (response: Response) => void
  const pending = new Promise<Response>((resolve) => { resolveRequest = resolve })
  const fetchMock = vi.fn().mockReturnValue(pending)
  vi.stubGlobal("fetch", fetchMock)
  const rendered = render(TradingSessionStatus)

  rendered.unmount()
  resolveRequest(Response.json({ status: "OPEN", total: 1, open: 1, closed: 0, unavailable: 0 }))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(60_000)

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

it("skips overlapping status requests and retries after a failed refresh", async () => {
  vi.useFakeTimers()
  const open = { status: "OPEN", total: 1, open: 1, closed: 0, unavailable: 0 }
  let rejectRefresh!: (reason: Error) => void
  const pending = new Promise<Response>((_resolve, reject) => { rejectRefresh = reject })
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json(open))
    .mockReturnValueOnce(pending)
    .mockImplementation(() => Promise.resolve(Response.json(open)))
  vi.stubGlobal("fetch", fetchMock)
  render(TradingSessionStatus)
  await flushPromises()

  await vi.advanceTimersByTimeAsync(60_000)
  await vi.advanceTimersByTimeAsync(120_000)
  expect(fetchMock).toHaveBeenCalledTimes(2)

  rejectRefresh(new Error("temporary failure"))
  await flushPromises()
  expect(screen.getByText("Статус торгов недоступен")).toBeTruthy()
  await vi.advanceTimersByTimeAsync(60_000)
  await flushPromises()
  expect(fetchMock).toHaveBeenCalledTimes(3)
  expect(screen.getByText("Торги доступны: 1/1")).toBeTruthy()
})
