import { expect, it } from "vitest"

import type { HistoricCandle } from "../api/marketData"
import { buildCandlestickGeometry } from "./candlestickGeometry"

const candles: HistoricCandle[] = [
  { instrument_id: "uid-1", open: "10", high: "14", low: "9", close: "13", volume: 100, started_at: "2026-08-05T09:58:00Z", is_complete: true },
  { instrument_id: "uid-1", open: "13", high: "13.5", low: "8", close: "9", volume: 200, started_at: "2026-08-05T09:59:00Z", is_complete: true },
  { instrument_id: "uid-1", open: "11", high: "12", low: "10", close: "11", volume: 50, started_at: "2026-08-05T10:00:00Z", is_complete: true },
]

it("maps rising, falling and flat candles to visible SVG geometry", () => {
  const geometry = buildCandlestickGeometry(candles)

  expect(geometry.shapes).toHaveLength(3)
  expect(geometry.shapes[0].direction).toBe("up")
  expect(geometry.shapes[1].direction).toBe("down")
  expect(geometry.shapes[2].bodyHeight).toBeGreaterThanOrEqual(1)
  expect(geometry.shapes[0].wickTop).toBeLessThan(geometry.shapes[0].wickBottom)
  expect(geometry.shapes[1].volumeHeight).toBeGreaterThan(geometry.shapes[0].volumeHeight)
  expect(geometry.width).toBeGreaterThan(3 * geometry.candleStep)
})

it("keeps coordinates finite when every price and volume is equal", () => {
  const flat = candles.map((item) => ({
    ...item,
    open: "10",
    high: "10",
    low: "10",
    close: "10",
    volume: 0,
  }))

  const geometry = buildCandlestickGeometry(flat)

  for (const shape of geometry.shapes) {
    expect(shape.bodyHeight).toBeGreaterThanOrEqual(1)
    expect([
      shape.x,
      shape.bodyX,
      shape.bodyY,
      shape.wickTop,
      shape.wickBottom,
      shape.volumeY,
      shape.volumeHeight,
    ].every(Number.isFinite)).toBe(true)
  }
  expect(geometry.latestClose.y).toBeGreaterThan(geometry.priceTicks[0].y)
  expect(geometry.latestClose.y).toBeLessThan(geometry.priceTicks.at(-1)!.y)
})

it("builds a right price axis and marks the latest completed close", () => {
  const geometry = buildCandlestickGeometry(candles)

  expect(geometry.priceTicks).toHaveLength(5)
  expect(geometry.priceTicks[0].value).toBe(14)
  expect(geometry.priceTicks.at(-1)?.value).toBe(8)
  expect(geometry.latestClose.value).toBe(11)
  expect(geometry.latestClose.label).toBe("11.00")
  expect(geometry.latestClose.y).toBeGreaterThan(geometry.priceTicks[0].y)
  expect(geometry.latestClose.y).toBeLessThan(geometry.priceTicks.at(-1)!.y)
})

it("keeps the average position price inside the price domain", () => {
  const geometry = buildCandlestickGeometry(candles, 20)

  expect(geometry.averagePrice).toEqual({ value: 20, y: 16, label: "20.00" })
  expect(geometry.priceTicks[0].value).toBe(20)
  expect(geometry.shapes.every((shape) => shape.wickTop >= 16)).toBe(true)
})

it("ignores a missing or invalid average position price", () => {
  const withoutAverage = buildCandlestickGeometry(candles)
  const withInvalidAverage = buildCandlestickGeometry(candles, Number.NaN)

  expect(withoutAverage.averagePrice).toBeNull()
  expect(withInvalidAverage.averagePrice).toBeNull()
  expect(withInvalidAverage.priceTicks).toEqual(withoutAverage.priceTicks)
  expect(withInvalidAverage.shapes).toEqual(withoutAverage.shapes)
})
