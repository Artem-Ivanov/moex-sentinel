import type { CandleInterval, HistoricCandle } from "../api/marketData"

interface CandleCacheEntry {
  version: 1
  fetched_at: string
  items: HistoricCandle[]
}

export function buildCandleCacheKey(
  brokerId: string,
  instrumentId: string,
  interval: CandleInterval,
): string {
  return `candles:${brokerId}:${instrumentId}:${interval}`
}

function isCandleCacheEntry(value: unknown): value is CandleCacheEntry {
  if (typeof value !== "object" || value === null) return false
  const candidate = value as Partial<CandleCacheEntry>
  return candidate.version === 1
    && typeof candidate.fetched_at === "string"
    && Array.isArray(candidate.items)
}

function removeInvalidEntry(storage: Storage, key: string): void {
  try {
    storage.removeItem(key)
  } catch {
    // Cache cleanup must not block a fresh API request.
  }
}

export function readCandleCache(
  storage: Storage | null,
  brokerId: string,
  instrumentId: string,
  interval: CandleInterval,
): HistoricCandle[] | null {
  if (storage === null) return null
  const key = buildCandleCacheKey(brokerId, instrumentId, interval)
  try {
    const serialized = storage.getItem(key)
    if (serialized === null) return null
    const parsed: unknown = JSON.parse(serialized)
    if (!isCandleCacheEntry(parsed)) {
      removeInvalidEntry(storage, key)
      return null
    }
    return parsed.items
  } catch {
    removeInvalidEntry(storage, key)
    return null
  }
}

export function writeCandleCache(
  storage: Storage | null,
  brokerId: string,
  instrumentId: string,
  interval: CandleInterval,
  items: HistoricCandle[],
  fetchedAt: string,
): void {
  if (storage === null) return
  try {
    const entry: CandleCacheEntry = { version: 1, fetched_at: fetchedAt, items }
    storage.setItem(buildCandleCacheKey(brokerId, instrumentId, interval), JSON.stringify(entry))
  } catch {
    // Cache failures must not affect live candle data.
  }
}
