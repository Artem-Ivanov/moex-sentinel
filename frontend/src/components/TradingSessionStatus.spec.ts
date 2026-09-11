import { cleanup, render, screen } from "@testing-library/vue"
import { afterEach, expect, it, vi } from "vitest"

import TradingSessionStatus from "./TradingSessionStatus.vue"

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it("shows aggregated live trading session status", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
    status: "OPEN", total: 2, open: 1, closed: 1, unavailable: 0,
  })))

  render(TradingSessionStatus)

  expect(await screen.findByText("Торги доступны: 1/2")).toBeTruthy()
})
