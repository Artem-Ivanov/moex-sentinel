import { cleanup, render, screen } from "@testing-library/vue"
import { afterEach, expect, it } from "vitest"

import type { TradingSummaryResponse } from "../api/portfolio"
import TradingSummary from "./TradingSummary.vue"

afterEach(cleanup)

const summary: TradingSummaryResponse = {
  captured_at: "2026-08-15T10:00:00Z",
  currencies: [
    {
      currency: "RUB",
      portfolio_value: "150.00",
      free_cash: "25.00",
      pnl_24h: { value: "12.50", from: "2026-08-14T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: false },
      pnl_7d: { value: "-2.00", from: "2026-08-08T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: true },
      pnl_30d: { value: null, from: null, to: "2026-08-15T10:00:00Z", complete: false },
    },
    {
      currency: "USD",
      portfolio_value: "20.00",
      free_cash: "3.00",
      pnl_24h: { value: "0.00", from: "2026-08-14T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: true },
      pnl_7d: { value: "1.00", from: "2026-08-08T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: true },
      pnl_30d: { value: "3.00", from: "2026-07-16T10:00:00Z", to: "2026-08-15T10:00:00Z", complete: true },
    },
  ],
  errors: [
    { broker_id: "broker-1", broker_name: "Synthetic broker", account_id: "account-1", code: "BROKER_UNAVAILABLE", message: "Недоступно" },
  ],
}

it("shows separate currency cards and signed period states", () => {
  render(TradingSummary, { props: { summary } })

  expect(screen.getByRole("heading", { name: "RUB" })).toBeTruthy()
  expect(screen.getByRole("heading", { name: "USD" })).toBeTruthy()
  const positive = screen.getByText(/\+12,50/)
  const negative = screen.getByText(/−2,00/)
  const zero = screen.getByText(/^0,00/)
  expect(positive.className).toContain("pnl--positive")
  expect(negative.className).toContain("pnl--negative")
  expect(zero.className).toContain("pnl--neutral")
  expect(screen.getByText("данные с 14.08.2026")).toBeTruthy()
  expect(screen.getByText("—")).toBeTruthy()
  expect(screen.getByText("Synthetic broker: Недоступно")).toBeTruthy()
})

it("shows an explicit empty state", () => {
  render(TradingSummary, {
    props: { summary: { captured_at: null, currencies: [], errors: [] } },
  })

  expect(screen.getByText("Снимки портфеля ещё не собраны")).toBeTruthy()
})
