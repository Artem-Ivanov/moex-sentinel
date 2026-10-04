<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue"
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
  if (data.value?.bootstrap_pending) return undefined
  const rawValue = data.value?.average_price
  if (typeof rawValue !== "string" || rawValue.trim() === "") return undefined
  const value = Number(rawValue)
  return Number.isFinite(value) ? value : undefined
})
const error = ref("")
const loading = ref(false)

let refreshTimer: ReturnType<typeof setInterval> | undefined
let disposed = false
let generation = 0

function isCurrent(requestGeneration: number): boolean {
  return !disposed && requestGeneration === generation
}

async function refresh(): Promise<void> {
  if (disposed || loading.value) return
  const requestGeneration = generation
  const automationId = String(route.params.id)
  loading.value = true
  error.value = ""
  try {
    const response = await fetchAutomationDetails(automationId)
    if (isCurrent(requestGeneration)) details.value = response
  } catch (caught: unknown) {
    if (isCurrent(requestGeneration)) error.value = caught instanceof Error ? caught.message : "Торговый автомат не найден."
  } finally {
    if (isCurrent(requestGeneration)) loading.value = false
  }
}

watch(() => String(route.params.id ?? ""), async (automationId) => {
  const requestGeneration = ++generation
  if (refreshTimer !== undefined) clearInterval(refreshTimer)
  details.value = undefined
  error.value = ""
  loading.value = false
  if (disposed || !automationId) return
  await refresh()
  // A prior route or unmounted page must not resurrect its polling timer.
  if (isCurrent(requestGeneration)) refreshTimer = setInterval(refresh, 60_000)
}, { immediate: true, flush: "sync" })
onBeforeUnmount(() => {
  disposed = true
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
      <p v-if="data.bootstrap_pending" role="status">Ожидает первоначальной сверки позиции</p>
      <div class="detail-grid">
        <article class="grid-row"><strong>Позиция</strong><p>Лоты: {{ data.bootstrap_pending ? '—' : data.quantity_lots }}</p><p>Средняя цена: {{ data.bootstrap_pending ? '—' : `${data.average_price} ${data.currency}` }}</p><p>Вложено: {{ data.bootstrap_pending ? '—' : `${data.invested_amount} ${data.currency}` }}</p></article>
        <article class="grid-row"><strong>Результат</strong><p>Realized: {{ data.bootstrap_pending ? '—' : data.realized_pnl }}</p><p>Unrealized: {{ data.bootstrap_pending ? '—' : data.unrealized_pnl }}</p><p>Net P&amp;L: {{ data.bootstrap_pending ? '—' : data.net_pnl }}</p><p>Комиссии: {{ data.bootstrap_pending ? '—' : data.actual_commissions }}</p></article>
        <article class="grid-row"><strong>Стратегия</strong><p>Код: {{ data.strategy_code }}</p><p>Версия: {{ data.strategy_version }}</p><p>Единый алгоритм для всех позиций. Пороги усреднения и частичной прибыли пересчитываются по рынку.</p><p>Revision: {{ data.revision }}</p></article>
      </div>
      <article class="candle-panel">
        <div class="candle-panel__header"><strong>Минутные свечи за последние два часа</strong></div>
        <div class="candle-chart-frame">
          <p v-if="details?.errors.some(item => item.source === 'candles')" class="error">{{ details.errors.find(item => item.source === 'candles')?.message }}</p>
          <p v-if="!loading && details?.candles.length === 0 && !details.errors.some(item => item.source === 'candles')" class="empty">Завершённых свечей за последние два часа нет</p>
          <CandlestickChart v-if="details?.candles.length" :candles="details.candles" :average-price="averagePrice" aria-label="Минутные свечи за последние два часа" />
          <div v-if="loading" class="candle-loading-overlay" role="status" aria-label="Обновление данных"><span class="candle-spinner" aria-hidden="true"></span></div>
        </div>
      </article>
      <article class="candle-panel">
        <div class="candle-panel__header"><strong>Брокерские операции по позиции</strong></div>
        <p v-if="details?.errors.some(item => item.source === 'operations')" class="error">{{ details.errors.find(item => item.source === 'operations')?.message }}</p>
        <p v-if="details?.operations.length === 0 && !details.errors.some(item => item.source === 'operations')" class="empty">Исполненных операций пока нет</p>
        <div v-if="details?.operations.length" class="table-scroll">
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
