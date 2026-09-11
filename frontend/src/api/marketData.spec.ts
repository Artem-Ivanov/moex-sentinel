import { afterEach, expect, it, vi } from "vitest"

import { fetchHistoricCandles, searchMarketInstruments } from "./marketData"

afterEach(() => vi.unstubAllGlobals())

it("sends broker-scoped search parameters without frontend normalization", async () => {
  const payload = { items: [] }
  const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
  vi.stubGlobal("fetch", fetchMock)

  await expect(searchMarketInstruments("broker-1", "  sber  ", 20)).resolves.toEqual(payload)

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/brokers/broker-1/market/instruments?query=++sber++&limit=20",
  )
})

it("requests broker-scoped historic candles without changing the range", async () => {
  const payload = { items: [{
    instrument_id: "uid-1",
    open: "310",
    high: "313",
    low: "309.5",
    close: "312.45",
    volume: 1250,
    started_at: "2026-08-05T09:59:00Z",
    is_complete: true,
  }] }
  const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
  vi.stubGlobal("fetch", fetchMock)

  await expect(fetchHistoricCandles(
    "broker-1",
    "uid-1",
    "2026-08-05T09:00:00.000Z",
    "2026-08-05T10:00:00.000Z",
    "1_MIN",
  )).resolves.toEqual(payload)

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/brokers/broker-1/market/instruments/uid-1/candles"
      + "?from=2026-08-05T09%3A00%3A00.000Z"
      + "&to=2026-08-05T10%3A00%3A00.000Z&interval=1_MIN",
  )
})
