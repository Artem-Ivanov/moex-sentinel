<script setup lang="ts">
import { computed } from "vue"

import type { HistoricCandle } from "../api/marketData"
import { buildCandlestickGeometry } from "./candlestickGeometry"

const props = withDefaults(defineProps<{ candles: HistoricCandle[]; ariaLabel?: string; averagePrice?: number }>(), {
  ariaLabel: "Минутные свечи за последние два часа",
})
const geometry = computed(() => buildCandlestickGeometry(props.candles, props.averagePrice))
</script>

<template>
  <div class="candlestick-chart__scroll">
    <svg
      class="candlestick-chart"
      role="img"
      :aria-label="props.ariaLabel"
      :viewBox="`0 0 ${geometry.width} ${geometry.height}`"
      :width="geometry.width"
      :height="geometry.height"
    >
      <g class="candlestick-chart__price-scale">
        <g v-for="tick in geometry.priceTicks" :key="tick.value">
          <line
            class="candlestick-chart__price-grid"
            x1="0"
            :x2="geometry.priceAxisX"
            :y1="tick.y"
            :y2="tick.y"
          />
          <text
            class="candlestick-chart__price-label"
            :x="geometry.priceLabelX"
            :y="tick.y + 4"
          >{{ tick.label }}</text>
        </g>
        <line
          class="candlestick-chart__price-axis"
          :x1="geometry.priceAxisX"
          :x2="geometry.priceAxisX"
          y1="16"
          y2="176"
        />
      </g>
      <g v-for="shape in geometry.shapes" :key="shape.key">
        <line
          class="candlestick-chart__wick"
          :class="`candlestick-chart__wick--${shape.direction}`"
          :x1="shape.x"
          :x2="shape.x"
          :y1="shape.wickTop"
          :y2="shape.wickBottom"
        />
        <rect
          class="candlestick-chart__body"
          :class="`candlestick-chart__body--${shape.direction}`"
          :x="shape.bodyX"
          :y="shape.bodyY"
          :width="geometry.bodyWidth"
          :height="shape.bodyHeight"
        />
        <rect
          class="candlestick-chart__volume"
          :class="`candlestick-chart__volume--${shape.direction}`"
          :x="shape.bodyX"
          :y="shape.volumeY"
          :width="geometry.bodyWidth"
          :height="shape.volumeHeight"
        />
        <text
          v-if="shape.timeLabel"
          class="candlestick-chart__label"
          :x="shape.x"
          y="248"
          text-anchor="middle"
        >{{ shape.timeLabel }}</text>
      </g>
      <g v-if="geometry.hasLatestClose" class="candlestick-chart__current-price">
        <line
          class="candlestick-chart__current-price-line"
          x1="0"
          :x2="geometry.priceAxisX"
          :y1="geometry.latestClose.y"
          :y2="geometry.latestClose.y"
        />
        <text
          class="candlestick-chart__current-price-label"
          :x="geometry.priceLabelX"
          :y="geometry.latestClose.y - 5"
        >{{ geometry.latestClose.label }}</text>
      </g>
      <g v-if="geometry.averagePrice" class="candlestick-chart__average-price">
        <line
          class="candlestick-chart__average-price-line"
          x1="0"
          :x2="geometry.priceAxisX"
          :y1="geometry.averagePrice.y"
          :y2="geometry.averagePrice.y"
        />
        <text
          class="candlestick-chart__average-price-label"
          :x="geometry.priceLabelX"
          :y="geometry.averagePrice.y + 14"
        >{{ geometry.averagePrice.label }}</text>
      </g>
    </svg>
  </div>
</template>
