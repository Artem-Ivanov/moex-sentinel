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
