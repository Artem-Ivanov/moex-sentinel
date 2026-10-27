import { expect, it } from "vitest"

import type { HistoricCandle } from "../api/marketData"
import {
  buildCandleCacheKey,
  readCandleCache,
  writeCandleCache,
} from "./candleCache"

const candles: HistoricCandle[] = [{
  instrument_id: "uid-1",
  open: "310",
  high: "313",
  low: "309.5",
  close: "312.45",
  volume: 1250,
  started_at: "2026-08-05T09:59:00Z",
  is_complete: true,
}]

function createStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key) },
    setItem: (key, value) => { values.set(key, value) },
  }
}

it("isolates cached candles by broker, instrument, and interval", () => {
  const storage = createStorage()

  expect(buildCandleCacheKey("broker-1", "uid-1", "1_MIN"))
    .toBe("candles:broker-1:uid-1:1_MIN")

  writeCandleCache(storage, "broker-1", "uid-1", "1_MIN", candles, "2026-08-05T10:00:00.000Z")

  expect(readCandleCache(storage, "broker-1", "uid-1", "1_MIN")).toEqual(candles)
  expect(readCandleCache(storage, "broker-2", "uid-1", "1_MIN")).toBeNull()
  expect(readCandleCache(storage, "broker-1", "uid-2", "1_MIN")).toBeNull()
  expect(readCandleCache(storage, "broker-1", "uid-1", "5_MIN")).toBeNull()
})

it("preserves a successful empty candle response", () => {
  const storage = createStorage()

  writeCandleCache(storage, "broker-1", "uid-1", "1_MIN", [], "2026-08-05T10:00:00.000Z")

  expect(readCandleCache(storage, "broker-1", "uid-1", "1_MIN")).toEqual([])
})

it.each([
  ["malformed JSON", "not-json"],
  ["unsupported version", JSON.stringify({ version: 2, fetched_at: "2026-08-05T10:00:00.000Z", items: candles })],
  ["missing items", JSON.stringify({ version: 1, fetched_at: "2026-08-05T10:00:00.000Z" })],
])("removes a cache entry with %s", (_caseName, value) => {
  const storage = createStorage()
  const key = "candles:broker-1:uid-1:1_MIN"
  storage.setItem(key, value)

  expect(readCandleCache(storage, "broker-1", "uid-1", "1_MIN")).toBeNull()
  expect(storage.getItem(key)).toBeNull()
})

it("treats unavailable storage and browser storage failures as cache misses", () => {
  const failingStorage = {
    ...createStorage(),
    getItem: () => { throw new Error("storage unavailable") },
    removeItem: () => { throw new Error("storage unavailable") },
    setItem: () => { throw new Error("storage unavailable") },
  }

  expect(readCandleCache(null, "broker-1", "uid-1", "1_MIN")).toBeNull()
  expect(readCandleCache(failingStorage, "broker-1", "uid-1", "1_MIN")).toBeNull()
  expect(() => writeCandleCache(null, "broker-1", "uid-1", "1_MIN", candles, "2026-08-05T10:00:00.000Z")).not.toThrow()
  expect(() => writeCandleCache(failingStorage, "broker-1", "uid-1", "1_MIN", candles, "2026-08-05T10:00:00.000Z")).not.toThrow()
})
