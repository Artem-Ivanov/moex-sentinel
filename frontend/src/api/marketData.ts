import { readErrorPayload, type ApiFieldError } from "./errorPayload"

export interface MarketLastPrice {
  price: string
  captured_at: string
}

export interface MarketInstrument {
  instrument_id: string
  figi: string
  ticker: string
  name: string
  class_code: string
  instrument_type: string
  currency: string | null
  lot: number
  api_trade_available: boolean
  last_price: MarketLastPrice | null
}

export interface MarketInstrumentSearchResponse {
  items: MarketInstrument[]
}

export type CandleInterval = "1_MIN" | "5_MIN" | "15_MIN" | "HOUR" | "DAY"

export interface HistoricCandle {
  instrument_id: string
  open: string
  high: string
  low: string
  close: string
  volume: number
  started_at: string
  is_complete: boolean
}

export interface HistoricCandlesResponse {
  items: HistoricCandle[]
}

export class MarketDataApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly fields: ApiFieldError[] = [],
  ) {
    super(message)
  }
}

async function ensureSuccess(response: Response): Promise<Response> {
  if (response.ok) return response
  const payload = await readErrorPayload(response)
  const detail = payload?.detail
  if (detail?.code && detail.message) {
    throw new MarketDataApiError(detail.code, detail.message, detail.fields ?? [])
  }
  throw new MarketDataApiError("MARKET_DATA_API_ERROR", "Не удалось получить рыночные данные.")
}

export async function searchMarketInstruments(
  brokerId: string,
  query: string,
  limit: number,
): Promise<MarketInstrumentSearchResponse> {
  const params = new URLSearchParams({ query, limit: String(limit) })
  const response = await ensureSuccess(
    await fetch(`/api/brokers/${brokerId}/market/instruments?${params.toString()}`),
  )
  return (await response.json()) as MarketInstrumentSearchResponse
}

export async function fetchHistoricCandles(
  brokerId: string,
  instrumentId: string,
  from: string,
  to: string,
  interval: CandleInterval,
): Promise<HistoricCandlesResponse> {
  const params = new URLSearchParams({ from, to, interval })
  const response = await ensureSuccess(
    await fetch(
      `/api/brokers/${brokerId}/market/instruments/${instrumentId}/candles?${params.toString()}`,
    ),
  )
  return (await response.json()) as HistoricCandlesResponse
}
