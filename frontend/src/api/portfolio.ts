export interface Money { amount: string; currency: string }
export interface ReadError { broker_id: string; broker_name: string; account_id: string | null; code: string; message: string }
export interface Account { broker_id: string; broker_name: string; account_id: string; name: string; status: string; account_type: string; total_amount: Money | null; free_cash: Money | null; realized_pnl: Money | null; unrealized_pnl: Money | null }
export interface AccountsResponse { accounts: Account[]; total_amounts: Money[]; total_free_cash: Money[]; errors: ReadError[] }
export interface Position { broker_id: string; broker_name: string; account_id: string; instrument_id: string; ticker: string; quantity: string; average_price: Money | null; current_price: Money | null; expected_yield: Money | null }
export interface PositionsResponse { items: Position[]; errors: ReadError[] }
export interface Operation { broker_id: string; broker_name: string; operation_id: string; account_id: string; instrument_id: string | null; ticker: string | null; operation_type: string; state: string; occurred_at: string; payment: Money | null; price: Money | null; quantity: string; commission: Money | null }
export interface OperationsResponse { items: Operation[]; errors: ReadError[] }
export interface TradingPnlPeriod { value: string | null; from: string | null; to: string | null; complete: boolean }
export interface CurrencyTradingSummary { currency: string; portfolio_value: string; free_cash: string; pnl_24h: TradingPnlPeriod; pnl_7d: TradingPnlPeriod; pnl_30d: TradingPnlPeriod }
export interface TradingSummaryResponse { captured_at: string | null; currencies: CurrencyTradingSummary[]; errors: ReadError[] }

async function load<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) throw new Error("Не удалось загрузить данные площадки.")
  return (await response.json()) as T
}

export const fetchBrokerAccounts = (brokerId: string) => load<AccountsResponse>(`/api/brokers/${brokerId}/accounts`)
export const fetchPortfolioSummary = () => load<AccountsResponse>("/api/portfolio/summary")
export const fetchPositions = () => load<PositionsResponse>("/api/positions")
export const fetchOperations = (limit = 20) => load<OperationsResponse>(`/api/operations?limit=${limit}`)
export const fetchTradingSummary = () => load<TradingSummaryResponse>("/api/trading/summary")
