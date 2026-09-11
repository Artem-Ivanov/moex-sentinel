import { afterEach, describe, expect, it, vi } from "vitest"

import {
  createAutomation,
  fetchAutomation,
  fetchAutomations,
  holdAutomation,
  resumeAutomation,
  closeAutomation,
} from "./automations"

afterEach(() => vi.unstubAllGlobals())

describe("automation API", () => {
  it("uses the agreed lifecycle endpoints", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [] }),
    })
    vi.stubGlobal("fetch", fetchMock)

    await createAutomation("broker-1", "instrument-1", "account-1")
    await fetchAutomations()
    await fetchAutomation("automation-1")
    await holdAutomation("automation-1")
    await resumeAutomation("automation-1")
    await closeAutomation("automation-1")

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/instruments/broker-1/instrument-1/trade",
      "/api/trading-automations",
      "/api/trading-automations/automation-1",
      "/api/trading-automations/automation-1/hold",
      "/api/trading-automations/automation-1/resume",
      "/api/trading-automations/automation-1/close",
    ])
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ account_id: "account-1" }),
    })
  })
})
