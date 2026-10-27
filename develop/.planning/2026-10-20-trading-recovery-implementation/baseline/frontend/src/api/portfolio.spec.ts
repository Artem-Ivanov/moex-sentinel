import { afterEach, describe, expect, it, vi } from "vitest"

import {
  fetchBrokerAccounts,
  fetchOperations,
  fetchPortfolioSummary,
  fetchPositions,
  fetchTradingSummary,
} from "./portfolio"

describe("portfolio API client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it.each([
    [() => fetchBrokerAccounts("broker-1"), "/api/brokers/broker-1/accounts"],
    [fetchPortfolioSummary, "/api/portfolio/summary"],
    [fetchPositions, "/api/positions"],
    [() => fetchOperations(25), "/api/operations?limit=25"],
    [fetchTradingSummary, "/api/trading/summary"],
  ])("loads read-only data from %s", async (loader, url) => {
    const payload = { items: [], errors: [] }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal("fetch", fetchMock)

    await expect(loader()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(url)
  })
})
