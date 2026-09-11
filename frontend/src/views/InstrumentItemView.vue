<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue"
import { RouterLink, useRoute, useRouter } from "vue-router"

import { fetchHistoricCandles, type HistoricCandle } from "../api/marketData"
import { fetchInstrumentDetails, type InstrumentDetails } from "../api/instruments"
import { fetchBrokerAccounts, type Account } from "../api/portfolio"
import { createAutomation } from "../api/automations"
import CandlestickChart from "../components/CandlestickChart.vue"
import { readCandleCache, writeCandleCache } from "../storage/candleCache"

const route = useRoute()
const router = useRouter()
const data = ref<InstrumentDetails>()
const error = ref("")
const candles = ref<HistoricCandle[]>([])
const candlesLoading = ref(false)
const candlesError = ref("")
const tradeOpen = ref(false)
const accounts = ref<Account[]>([])
const selectedAccount = ref("")
const tradeError = ref("")
const tradeLoading = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | undefined
let disposed = false

async function openTrade(): Promise<void> {
  tradeOpen.value = true
  tradeLoading.value = true
  tradeError.value = ""
  try {
    const response = await fetchBrokerAccounts(String(route.params.brokerId))
    accounts.value = response.accounts
    selectedAccount.value = response.accounts[0]?.account_id ?? ""
  } catch { tradeError.value = "Не удалось загрузить счета." }
  finally { tradeLoading.value = false }
}

async function startTrade(): Promise<void> {
  if (!selectedAccount.value) return
  tradeLoading.value = true
  tradeError.value = ""
  try {
    const automation = await createAutomation(
      String(route.params.brokerId), String(route.params.instrumentId), selectedAccount.value,
    )
    await router.push({ name: "position-details", params: { id: automation.id } })
  } catch (caught: unknown) { tradeError.value = caught instanceof Error ? caught.message : "Не удалось создать автомат." }
  finally { tradeLoading.value = false }
}

function getSessionStorage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

async function loadCandles(brokerId: string, instrumentId: string): Promise<void> {
  if (disposed || candlesLoading.value) return
  const storage = getSessionStorage()
  const cached = readCandleCache(storage, brokerId, instrumentId, "1_MIN")
  const isRefresh = cached !== null
  candles.value = currentWindow(cached ?? candles.value, instrumentId)
  candlesLoading.value = true
  candlesError.value = ""
  const end = new Date()
  const start = new Date(end.getTime() - 2 * 60 * 60 * 1000)
  try {
    const response = await fetchHistoricCandles(
      brokerId,
      instrumentId,
      start.toISOString(),
      end.toISOString(),
      "1_MIN",
    )
    if (disposed) return
    candles.value = currentWindow(response.items, instrumentId)
    writeCandleCache(
      storage,
      brokerId,
      instrumentId,
      "1_MIN",
      candles.value,
      new Date().toISOString(),
    )
  } catch {
    if (disposed) return
    candlesError.value = isRefresh
      ? "Не удалось обновить свечи."
      : "Не удалось загрузить свечи."
  } finally {
    candlesLoading.value = false
  }
}

function currentWindow(items: HistoricCandle[], instrumentId: string): HistoricCandle[] {
  const end = Date.now()
  const start = end - 2 * 60 * 60 * 1000
  return items.filter((item) => {
    const startedAt = Date.parse(item?.started_at)
    return item?.is_complete && item.instrument_id === instrumentId && startedAt >= start && startedAt < end
  })
}

onMounted(async () => {
  const brokerId = String(route.params.brokerId)
  const instrumentId = String(route.params.instrumentId)
  refreshTimer = setInterval(() => {
    if (data.value) void loadCandles(brokerId, data.value.instrument.instrument_id)
  }, 60_000)
  try {
    const details = await fetchInstrumentDetails(brokerId, instrumentId)
    if (disposed) return
    data.value = details
    await loadCandles(brokerId, details.instrument.instrument_id)
  } catch (caught: unknown) {
    error.value = caught instanceof Error ? caught.message : "Не удалось загрузить инструмент."
  }
})

onBeforeUnmount(() => {
  disposed = true
  if (refreshTimer !== undefined) clearInterval(refreshTimer)
})
</script>

<template>
  <section class="page">
    <RouterLink :to="{ name: 'instruments' }" class="hint">← К списку инструментов</RouterLink>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-else-if="!data">Загрузка…</p>
    <template v-else>
      <p class="eyebrow">{{ data.broker_name }}</p>
      <h2>{{ data.instrument.ticker }} — {{ data.instrument.name }}</h2>
      <div class="toolbar">
        <button
          :disabled="!data.instrument.is_active || !data.instrument.api_trade_available"
          @click="openTrade"
        >Торговля</button>
      </div>
      <article v-if="tradeOpen" class="grid-row trade-panel">
        <strong>Новый торговый автомат</strong>
        <p v-if="tradeLoading && accounts.length === 0">Загрузка счетов…</p>
        <label v-else>Счёт
          <select v-model="selectedAccount">
            <option v-for="account in accounts" :key="account.account_id" :value="account.account_id">{{ account.name }} — {{ account.account_id }}</option>
          </select>
        </label>
        <p v-if="tradeError" class="error">{{ tradeError }}</p>
        <button :disabled="tradeLoading || !selectedAccount" @click="startTrade">Создать автомат</button>
      </article>
      <div class="detail-grid">
        <article class="grid-row"><strong>Идентификаторы</strong><p>UID: {{ data.instrument.instrument_id }}</p><p>FIGI: {{ data.instrument.figi }}</p><p>Class code: {{ data.instrument.class_code }}</p></article>
        <article class="grid-row"><strong>Торговые свойства</strong><p>Тип: {{ data.instrument.category }}</p><p>Валюта: {{ data.instrument.currency ?? "—" }}</p><p>Лот: {{ data.instrument.lot }}</p></article>
        <article class="grid-row"><strong>Состояние</strong><p>{{ data.instrument.is_active ? "Активен" : "Снят с листинга" }}</p><p>{{ data.instrument.api_trade_available ? "Доступен через API" : "Недоступен через API" }}</p><p>{{ data.instrument.is_selected ? "Выделен для анализа" : "Не выделен" }}</p></article>
        <article class="grid-row"><strong>Текущая цена</strong><p>{{ data.last_price ? `${data.last_price.price} ${data.instrument.currency ?? ""}` : "Нет данных" }}</p><p>Цена лота: {{ data.lot_price ? `${data.lot_price} ${data.instrument.currency ?? ""}` : "—" }}</p><p>{{ data.last_price?.captured_at ?? "—" }}</p></article>
      </div>
      <p class="hint">Последняя синхронизация: {{ data.sync_state.last_success_at ?? "не выполнялась" }}</p>
      <article class="candle-panel">
        <div class="candle-panel__header">
          <strong>Минутные свечи за последние два часа</strong>
          <div class="candle-intervals" aria-label="Интервал свечей">
            <button type="button" class="filter-button--active" aria-pressed="true">1 мин</button>
            <button
              v-for="label in ['5 мин', '15 мин', '1 час', '1 день']"
              :key="label"
              type="button"
              disabled
              title="Будет доступно в следующей версии"
              aria-description="Будет доступно в следующей версии"
            >{{ label }}</button>
          </div>
        </div>
        <div class="candle-chart-frame">
          <p v-if="candlesError" class="error">{{ candlesError }}</p>
          <p v-if="!candlesLoading && !candlesError && candles.length === 0" class="empty">За последние два часа завершённых свечей нет</p>
          <CandlestickChart v-if="candles.length > 0" :candles="candles" />
          <div
            v-if="candlesLoading"
            class="candle-loading-overlay"
            role="status"
            aria-label="Обновление свечей"
          >
            <span class="candle-spinner" aria-hidden="true"></span>
            <span class="visually-hidden">Обновление свечей</span>
          </div>
        </div>
      </article>
    </template>
  </section>
</template>
