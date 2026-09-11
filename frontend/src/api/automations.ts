export interface Automation {
  id: string
  broker_id: string
  account_id: string
  instrument_id: string
  state: "IN_QUEUE" | "OPENING" | "IN_WORK" | "HOLD" | "CLOSED"
  suspended_from_state: Automation["state"] | null
  revision: number
  last_sequence_number: number
  resume_requested: boolean
  currency: string
  strategy_code: string
  strategy_version: string
  quantity_lots: number
  average_price: string
  invested_amount: string
  realized_pnl: string
  unrealized_pnl: string
  net_pnl: string
  actual_commissions: string
  broker_name: string
  ticker: string
  instrument_name: string
}

export interface AutomationList { items: Automation[] }
export interface TradingSessionsStatus { status: "OPEN" | "CLOSED" | "NO_ACTIVE" | "UNAVAILABLE"; total: number; open: number; closed: number; unavailable: number }

export interface PositionDetailsError { source: "operations" | "candles"; code: string; message: string }
export interface AutomationDetails {
  automation: Automation
  operations: import("./portfolio").Operation[]
  candles: import("./marketData").HistoricCandle[]
  errors: PositionDetailsError[]
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly fields: Array<{ path: string; code: string; message: string }> = [],
  ) { super(message) }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as {
      detail?: { message?: string; fields?: Array<{ path: string; code: string; message: string }> }
    }
    throw new ApiError(payload.detail?.message ?? "Не удалось выполнить запрос.", payload.detail?.fields)
  }
  if (response.status === 204) return undefined as T
  return await response.json() as T
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
})

export const createAutomation = (brokerId: string, instrumentId: string, accountId: string) =>
  request<Automation>(`/api/instruments/${brokerId}/${instrumentId}/trade`, json("POST", { account_id: accountId }))
export const fetchAutomations = () => request<AutomationList>("/api/trading-automations")
export const fetchTradingSessionsStatus = () => request<TradingSessionsStatus>("/api/trading-sessions/status")
export const fetchAutomation = (id: string) => request<Automation>(`/api/trading-automations/${id}`)
export const fetchAutomationDetails = (id: string) => request<AutomationDetails>(`/api/trading-automations/${id}/details`)
export const holdAutomation = (id: string) => request<Automation>(`/api/trading-automations/${id}/hold`, json("POST"))
export const resumeAutomation = (id: string) => request<Automation>(`/api/trading-automations/${id}/resume`, json("POST"))
export const closeAutomation = (id: string) => request<Automation>(`/api/trading-automations/${id}/close`, json("POST"))
