<script setup lang="ts">
import type { TradingPnlPeriod, TradingSummaryResponse } from "../api/portfolio"

defineProps<{ summary: TradingSummaryResponse }>()

const currency = (value: string, code: string) => new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: code,
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
}).format(Number(value))

const pnl = (value: string | null, code: string) => {
  if (value === null) return "—"
  const number = Number(value)
  const formatted = currency(String(Math.abs(number)), code)
  if (number > 0) return `+${formatted}`
  if (number < 0) return `−${formatted}`
  return formatted
}

const pnlClass = (value: string | null) => {
  if (value === null || Number(value) === 0) return "pnl--neutral"
  return Number(value) > 0 ? "pnl--positive" : "pnl--negative"
}

const actualStart = (period: TradingPnlPeriod) => {
  if (period.complete || !period.from) return ""
  return `данные с ${new Date(period.from).toLocaleDateString("ru-RU", { timeZone: "UTC" })}`
}

const periods = (item: TradingSummaryResponse["currencies"][number]) => [
  ["За сутки", item.pnl_24h],
  ["За 7 дней", item.pnl_7d],
  ["За месяц", item.pnl_30d],
] as const
</script>

<template>
  <div v-if="summary.currencies.length === 0" class="empty">Снимки портфеля ещё не собраны</div>
  <div v-else class="trading-summary-grid">
    <article v-for="item in summary.currencies" :key="item.currency" class="trading-summary-card">
      <h3>{{ item.currency }}</h3>
      <dl class="trading-summary-totals">
        <div><dt>Стоимость портфеля</dt><dd>{{ currency(item.portfolio_value, item.currency) }}</dd></div>
        <div><dt>Свободные средства</dt><dd>{{ currency(item.free_cash, item.currency) }}</dd></div>
      </dl>
      <div class="trading-summary-periods">
        <div v-for="([label, period]) in periods(item)" :key="label" class="trading-summary-period">
          <small>{{ label }}</small>
          <strong :class="pnlClass(period.value)">{{ pnl(period.value, item.currency) }}</strong>
          <span v-if="actualStart(period)" class="trading-summary-period__hint">{{ actualStart(period) }}</span>
        </div>
      </div>
    </article>
  </div>
  <p v-for="item in summary.errors" :key="item.code + item.account_id" class="error">
    {{ item.broker_name }}: {{ item.message }}
  </p>
</template>
