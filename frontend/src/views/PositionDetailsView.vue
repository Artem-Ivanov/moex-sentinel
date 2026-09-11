<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"
import { RouterLink, useRoute } from "vue-router"

import {
  fetchAutomationDetails,
  type AutomationDetails,
} from "../api/automations"
import CandlestickChart from "../components/CandlestickChart.vue"

const route = useRoute()
const details = ref<AutomationDetails>()
const data = computed(() => details.value?.automation)
const averagePrice = computed(() => {
  const rawValue = data.value?.average_price
  if (typeof rawValue !== "string" || rawValue.trim() === "") return undefined
  const value = Number(rawValue)
  return Number.isFinite(value) ? value : undefined
})
const error = ref("")
const loading = ref(false)

async function refresh(): Promise<void> {
  if (loading.value) return
  loading.value = true
  error.value = ""
  try { details.value = await fetchAutomationDetails(String(route.params.id)) }
  catch (caught: unknown) { error.value = caught instanceof Error ? caught.message : "Торговый автомат не найден." }
  finally { loading.value = false }
}

let refreshTimer: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  await refresh()
  refreshTimer = setInterval(refresh, 60_000)
})
onBeforeUnmount(() => {
  if (refreshTimer !== undefined) clearInterval(refreshTimer)
})

</script>

<template>
  <section class="page">
    <RouterLink :to="{ name: 'positions' }" class="hint">← К открытым позициям</RouterLink>
    <p class="eyebrow">Позиция / автомат</p>
    <h2>{{ data ? `${data.ticker || data.instrument_id} — торговый автомат` : "Детальная информация" }}</h2>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="!data && !error">Загрузка…</p>
    <template v-if="data">
      <div class="toolbar">
        <span class="status-pill">{{ data.state }}</span>
        <span>{{ data.broker_name || data.broker_id }} / {{ data.account_id }}</span>
        <button :disabled="loading" @click="refresh">Обновить</button>
      </div>
      <div class="detail-grid">
        <article class="grid-row"><strong>Позиция</strong><p>Лоты: {{ data.quantity_lots }}</p><p>Средняя цена: {{ data.average_price }} {{ data.currency }}</p><p>Вложено: {{ data.invested_amount }} {{ data.currency }}</p></article>
        <article class="grid-row"><strong>Результат</strong><p>Realized: {{ data.realized_pnl }}</p><p>Unrealized: {{ data.unrealized_pnl }}</p><p>Net P&amp;L: {{ data.net_pnl }}</p><p>Комиссии: {{ data.actual_commissions }}</p></article>
        <article class="grid-row"><strong>Стратегия</strong><p>Код: {{ data.strategy_code }}</p><p>Версия: {{ data.strategy_version }}</p><p>Единый алгоритм для всех позиций. Пороги усреднения и частичной прибыли пересчитываются по рынку.</p><p>Revision: {{ data.revision }}</p></article>
      </div>
      <article class="candle-panel">
        <div class="candle-panel__header"><strong>Минутные свечи за последние два часа</strong></div>
        <div class="candle-chart-frame">
          <p v-if="details?.errors.some(item => item.source === 'candles')" class="error">{{ details.errors.find(item => item.source === 'candles')?.message }}</p>
          <p v-if="!loading && details?.candles.length === 0" class="empty">Завершённых свечей за последние два часа нет</p>
          <CandlestickChart v-if="details?.candles.length" :candles="details.candles" :average-price="averagePrice" aria-label="Минутные свечи за последние два часа" />
          <div v-if="loading" class="candle-loading-overlay" role="status" aria-label="Обновление данных"><span class="candle-spinner" aria-hidden="true"></span></div>
        </div>
      </article>
      <article class="candle-panel">
        <div class="candle-panel__header"><strong>Брокерские операции по позиции</strong></div>
        <p v-if="details?.errors.some(item => item.source === 'operations')" class="error">{{ details.errors.find(item => item.source === 'operations')?.message }}</p>
        <p v-if="details?.operations.length === 0" class="empty">Исполненных операций пока нет</p>
        <div v-else class="table-scroll">
          <table class="data-table">
            <thead><tr><th>Время</th><th>Операция</th><th>Состояние</th><th>Количество</th><th>Цена</th><th>Сумма</th><th>Комиссия</th></tr></thead>
            <tbody><tr v-for="item in details?.operations" :key="item.operation_id">
              <td>{{ new Date(item.occurred_at).toLocaleString('ru-RU') }}</td><td>{{ item.operation_type }}</td><td>{{ item.state }}</td><td>{{ item.quantity }}</td>
              <td>{{ item.price ? `${item.price.amount} ${item.price.currency}` : '—' }}</td><td>{{ item.payment ? `${item.payment.amount} ${item.payment.currency}` : '—' }}</td><td>{{ item.commission ? `${item.commission.amount} ${item.commission.currency}` : '—' }}</td>
            </tr></tbody>
          </table>
        </div>
      </article>
    </template>
  </section>
</template>
