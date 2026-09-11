export interface CatalogInstrument {
  id: string
  broker_id: string
  instrument_id: string
  figi: string
  ticker: string
  name: string
  class_code: string
  instrument_type: string
  category: string
  currency: string | null
  lot: number
  min_price_increment: string
  api_trade_available: boolean
  is_active: boolean
  is_selected: boolean
  first_seen_at: string
  last_seen_at: string
}

export interface CatalogInstrumentPrice extends CatalogInstrument {
  unit_price: string | null
  lot_price: string | null
  price_captured_at: string | null
}

export interface CatalogCategory { code: string; label: string; count: number }
export interface CatalogSyncState { broker_id: string; status: string; last_attempt_at: string | null; last_success_at: string | null; safe_error: string | null }
export interface CatalogListResponse { broker_id: string; items: CatalogInstrumentPrice[]; categories: CatalogCategory[]; sync_state: CatalogSyncState; currencies: string[] }
export interface ReconciliationResult { broker_id: string; added: number; updated: number; deactivated: number; synchronized_at: string }
export interface InstrumentDetails { broker_name: string; instrument: CatalogInstrument; last_price: { price: string; captured_at: string } | null; lot_price: string | null; sync_state: CatalogSyncState }

async function load<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await (init ? fetch(url, init) : fetch(url))
  if (!response.ok) {
    try {
      const payload = (await response.json()) as { detail?: { message?: string } }
      throw new Error(payload.detail?.message ?? "Не удалось выполнить запрос.")
    } catch (error: unknown) {
      if (error instanceof Error) throw error
      throw new Error("Не удалось выполнить запрос.")
    }
  }
  return (await response.json()) as T
}

export function fetchInstruments(
  brokerId: string,
  category: string | null,
  includeInactive: boolean,
  selectedOnly: boolean,
  lotPriceFrom: string = "",
  lotPriceTo: string = "",
  currency: string = "RUB",
): Promise<CatalogListResponse> {
  const params = new URLSearchParams()
  if (category) params.set("category", category)
  params.set("include_inactive", String(includeInactive))
  params.set("selected_only", String(selectedOnly))
  if (lotPriceFrom.trim()) params.set("lot_price_from", lotPriceFrom.trim())
  if (lotPriceTo.trim()) params.set("lot_price_to", lotPriceTo.trim())
  params.set("currency", currency)
  return load(`/api/brokers/${brokerId}/instruments?${params.toString()}`)
}

export const fetchInstrumentDetails = (brokerId: string, instrumentId: string) =>
  load<InstrumentDetails>(`/api/brokers/${brokerId}/instruments/${instrumentId}`)

export const synchronizeInstruments = (brokerId: string) =>
  load<ReconciliationResult>(`/api/brokers/${brokerId}/instruments/synchronize`, { method: "POST" })

export const setInstrumentSelection = (brokerId: string, instrumentId: string, selected: boolean) =>
  load<CatalogInstrument>(`/api/brokers/${brokerId}/instruments/${instrumentId}/selection`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selected }),
  })
