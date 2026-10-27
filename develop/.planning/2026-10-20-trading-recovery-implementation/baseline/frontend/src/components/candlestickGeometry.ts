import type { HistoricCandle } from "../api/marketData"

const CANDLE_STEP = 18
const BODY_WIDTH = 10
const PRICE_TOP = 16
const PRICE_HEIGHT = 160
const VOLUME_TOP = 196
const VOLUME_HEIGHT = 40
const MIN_WIDTH = 720
const SIDE_PADDING = 20
const CHART_HEIGHT = 252
const PRICE_AXIS_WIDTH = 120
const PRICE_TICK_COUNT = 5

export interface CandleShape {
  key: string
  x: number
  bodyX: number
  bodyY: number
  bodyHeight: number
  wickTop: number
  wickBottom: number
  volumeY: number
  volumeHeight: number
  direction: "up" | "down"
  timeLabel: string | null
}

export interface CandlestickGeometry {
  width: number
  height: number
  candleStep: number
  bodyWidth: number
  priceAxisX: number
  priceLabelX: number
  priceTicks: PricePoint[]
  latestClose: PricePoint
  hasLatestClose: boolean
  averagePrice: PricePoint | null
  shapes: CandleShape[]
}

export interface PricePoint {
  value: number
  y: number
  label: string
}

function formatPrice(value: number): string {
  return value.toFixed(2)
}

export function buildCandlestickGeometry(
  candles: HistoricCandle[],
  averagePrice?: number,
): CandlestickGeometry {
  const values = candles.map((candle) => ({
    candle,
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
  }))
  const validAveragePrice = averagePrice !== undefined && Number.isFinite(averagePrice)
    ? averagePrice
    : null
  const candleMinimum = values.length > 0 ? Math.min(...values.map((item) => item.low)) : 0
  const candleMaximum = values.length > 0 ? Math.max(...values.map((item) => item.high)) : 1
  const observedMinimum = validAveragePrice === null
    ? candleMinimum
    : Math.min(candleMinimum, validAveragePrice)
  const observedMaximum = validAveragePrice === null
    ? candleMaximum
    : Math.max(candleMaximum, validAveragePrice)
  const flatPricePadding = Math.max(Math.abs(observedMaximum) * 0.005, 0.01)
  const minimum = observedMaximum === observedMinimum ? observedMinimum - flatPricePadding : observedMinimum
  const maximum = observedMaximum === observedMinimum ? observedMaximum + flatPricePadding : observedMaximum
  const priceRange = maximum - minimum
  const maxVolume = Math.max(1, ...candles.map((candle) => candle.volume))
  const priceY = (price: number) => PRICE_TOP + ((maximum - price) / priceRange) * PRICE_HEIGHT
  const plotWidth = Math.max(MIN_WIDTH - PRICE_AXIS_WIDTH, SIDE_PADDING * 2 + candles.length * CANDLE_STEP)
  const priceTicks = Array.from({ length: PRICE_TICK_COUNT }, (_, index): PricePoint => {
    const ratio = index / (PRICE_TICK_COUNT - 1)
    const value = maximum - priceRange * ratio
    return { value, y: PRICE_TOP + PRICE_HEIGHT * ratio, label: formatPrice(value) }
  })
  const latestCompleted = values.slice().reverse().find(({ candle }) => candle.is_complete)
  const latestCloseValue = latestCompleted?.close ?? 0

  const shapes = values.map(({ candle, open, high, low, close }, index): CandleShape => {
    const x = SIDE_PADDING + index * CANDLE_STEP + CANDLE_STEP / 2
    const openY = priceY(open)
    const closeY = priceY(close)
    const volumeHeight = (candle.volume / maxVolume) * VOLUME_HEIGHT
    const showTime = index === 0 || index === values.length - 1 || index % 10 === 0
    return {
      key: `${candle.started_at}-${index}`,
      x,
      bodyX: x - BODY_WIDTH / 2,
      bodyY: Math.min(openY, closeY),
      bodyHeight: Math.max(1, Math.abs(closeY - openY)),
      wickTop: priceY(high),
      wickBottom: priceY(low),
      volumeY: VOLUME_TOP + VOLUME_HEIGHT - volumeHeight,
      volumeHeight,
      direction: close >= open ? "up" : "down",
      timeLabel: showTime ? new Date(candle.started_at).toISOString().slice(11, 16) : null,
    }
  })

  return {
    width: plotWidth + PRICE_AXIS_WIDTH,
    height: CHART_HEIGHT,
    candleStep: CANDLE_STEP,
    bodyWidth: BODY_WIDTH,
    priceAxisX: plotWidth,
    priceLabelX: plotWidth + 8,
    priceTicks,
    latestClose: {
      value: latestCloseValue,
      y: priceY(latestCloseValue),
      label: formatPrice(latestCloseValue),
    },
    hasLatestClose: latestCompleted !== undefined,
    averagePrice: validAveragePrice === null ? null : {
      value: validAveragePrice,
      y: priceY(validAveragePrice),
      label: formatPrice(validAveragePrice),
    },
    shapes,
  }
}
