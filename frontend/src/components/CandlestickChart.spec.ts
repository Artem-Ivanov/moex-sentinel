import { cleanup, render, screen } from "@testing-library/vue"
import { afterEach, expect, it } from "vitest"

import type { HistoricCandle } from "../api/marketData"
import CandlestickChart from "./CandlestickChart.vue"

afterEach(cleanup)

it("renders rising and falling OHLCV candles in a scrollable SVG", () => {
  const candles: HistoricCandle[] = [
    { instrument_id: "uid-1", open: "10", high: "14", low: "9", close: "13", volume: 100, started_at: "2026-08-05T09:58:00Z", is_complete: true },
    { instrument_id: "uid-1", open: "13", high: "13.5", low: "8", close: "9", volume: 200, started_at: "2026-08-05T09:59:00Z", is_complete: true },
  ]

  const { container } = render(CandlestickChart, { props: { candles } })

  expect(screen.getByRole("img", { name: "Минутные свечи за последние два часа" })).toBeTruthy()
  expect(container.querySelectorAll(".candlestick-chart__wick")).toHaveLength(2)
  expect(container.querySelectorAll(".candlestick-chart__body--up")).toHaveLength(1)
  expect(container.querySelectorAll(".candlestick-chart__body--down")).toHaveLength(1)
  expect(container.querySelectorAll(".candlestick-chart__volume")).toHaveLength(2)
  expect(container.querySelectorAll(".candlestick-chart__price-grid")).toHaveLength(5)
  expect(container.querySelectorAll(".candlestick-chart__price-label")).toHaveLength(5)
  expect(container.querySelector(".candlestick-chart__current-price-line")).toBeTruthy()
  expect(screen.getByText("9.00")).toBeTruthy()
  expect(screen.queryByText(/Последняя:/)).toBeNull()
  expect(container.querySelector(".candlestick-chart__scroll")).toBeTruthy()
})

it("renders the average position price as a separate red numeric marker", () => {
  const candles: HistoricCandle[] = [
    { instrument_id: "uid-1", open: "10", high: "14", low: "9", close: "13", volume: 100, started_at: "2026-08-05T09:58:00Z", is_complete: true },
  ]

  const { container } = render(CandlestickChart, { props: { candles, averagePrice: 20 } })

  expect(container.querySelector(".candlestick-chart__average-price-label")?.textContent).toBe("20.00")
  expect(screen.queryByText(/Средняя:/)).toBeNull()
  expect(container.querySelector(".candlestick-chart__average-price-line")).toBeTruthy()
  expect(container.querySelector(".candlestick-chart__average-price-label")).toBeTruthy()
})

it("does not render an average marker when its value is invalid", () => {
  const candles: HistoricCandle[] = [
    { instrument_id: "uid-1", open: "10", high: "14", low: "9", close: "13", volume: 100, started_at: "2026-08-05T09:58:00Z", is_complete: true },
  ]

  const { container } = render(CandlestickChart, { props: { candles, averagePrice: Number.NaN } })

  expect(container.querySelector(".candlestick-chart__average-price-line")).toBeNull()
  expect(container.querySelector(".candlestick-chart__average-price-label")).toBeNull()
})
