<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { useRouter } from "vue-router"

import {
  closeAutomation,
  fetchAutomations,
  holdAutomation,
  resumeAutomation,
  type Automation,
  type AutomationList,
} from "../api/automations"
import {
  fetchOperations,
  fetchTradingSummary,
  type Money,
  type OperationsResponse,
  type TradingSummaryResponse,
} from "../api/portfolio"
import TradingSummary from "../components/TradingSummary.vue"

const router = useRouter()
const data = ref<AutomationList>()
const selectedId = ref<string>()
const selected = computed(() => data.value?.items.find((item) => item.id === selectedId.value))
const error = ref("")
const operations = ref<OperationsResponse>()
const operationsError = ref("")
const summary = ref<TradingSummaryResponse>()
const summaryError = ref("")
const limit = ref(20)
const refreshing = ref(false)
const actionPending = ref(false)
const money = (value: Money | null) => value ? `${value.amount} ${value.currency}` : "—"

async function loadPositions(): Promise<void> {
  error.value = ""
  try {
    data.value = await fetchAutomations()
    if (selectedId.value && !data.value.items.some((item) => item.id === selectedId.value)) selectedId.value = undefined
  }
  catch { error.value = "Не удалось загрузить торговые автоматы." }
}

async function loadOperations(): Promise<void> {
  operationsError.value = ""
  try { operations.value = await fetchOperations(limit.value) }
  catch { operationsError.value = "Не удалось загрузить операции." }
}

async function loadSummary(): Promise<void> {
  summaryError.value = ""
  try { summary.value = await fetchTradingSummary() }
  catch { summaryError.value = "Не удалось загрузить сводку." }
}

async function refresh(): Promise<void> {
  if (refreshing.value) return
  refreshing.value = true
  await Promise.allSettled([loadSummary(), loadPositions(), loadOperations()])
  refreshing.value = false
}

onMounted(refresh)

const details = () => selected.value && router.push({
  name: "position-details",
  params: { id: selected.value.id },
})

async function action(operation: (id: string) => Promise<Automation>): Promise<void> {
  if (!selected.value || actionPending.value || refreshing.value) return
  actionPending.value = true
  error.value = ""
  try {
    await operation(selected.value.id)
    await loadPositions()
  }
  catch (caught: unknown) { error.value = caught instanceof Error ? caught.message : "Не удалось выполнить действие." }
  finally { actionPending.value = false }
}
</script>

<template>
  <section class="page">
    <div class="page-title">
      <div><p class="eyebrow">Портфель и операции</p><h2>Торговля</h2></div>
      <button :disabled="refreshing" @click="refresh">Обновить</button>
    </div>
    <div class="page-title section-title">
      <h2>Сводка</h2>
    </div>
    <p v-if="summaryError" class="error">{{ summaryError }}</p>
    <p v-if="!summary && !summaryError">Загрузка…</p>
    <TradingSummary v-if="summary" :summary="summary" />
    <div class="page-title section-title">
      <h2>Открытые позиции</h2>
      <div class="toolbar">
        <button :disabled="!selected || actionPending || refreshing" @click="details">Подробнее</button>
        <button :disabled="!selected || actionPending || refreshing || !['IN_QUEUE', 'IN_WORK'].includes(selected.state)" @click="action(holdAutomation)">Hold</button>
        <button :disabled="!selected || actionPending || refreshing || selected.state !== 'HOLD' || selected.resume_requested" @click="action(resumeAutomation)">Resume</button>
        <button :disabled="!selected || actionPending || refreshing || selected.state === 'CLOSED'" @click="action(closeAutomation)">Закрыть автомат</button>
      </div>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-else-if="!data">Загрузка…</p>
    <div v-else-if="data.items.length === 0" class="empty">Активных автоматов нет</div>
    <div v-else class="table-scroll">
      <table class="data-table">
        <thead><tr><th>Тикер</th><th>Брокер</th><th>Счёт</th><th>Статус</th><th>Лоты</th><th>Средняя цена</th><th>Вложено</th><th>Net P&amp;L</th><th>Комиссии</th></tr></thead>
        <tbody>
          <tr
            v-for="item in data.items"
            :key="item.id"
            tabindex="0"
            :class="{ 'data-table__row--selected': selectedId === item.id }"
            @click="selectedId = item.id"
            @keydown.enter="selectedId = item.id"
            @dblclick="router.push({ name: 'position-details', params: { id: item.id } })"
          >
            <td>{{ item.ticker || item.instrument_id }}</td>
            <td>{{ item.broker_name || item.broker_id }}</td>
            <td>{{ item.account_id }}</td>
            <td><span class="status-pill">{{ item.state }}</span></td>
            <td>{{ item.quantity_lots }}</td>
            <td>{{ item.average_price }} {{ item.currency }}</td>
            <td>{{ item.invested_amount }} {{ item.currency }}</td>
            <td>{{ item.net_pnl }} {{ item.currency }}</td>
            <td>{{ item.actual_commissions }} {{ item.currency }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="page-title section-title">
      <h2>Последние операции</h2>
      <label>Показывать
        <select v-model.number="limit" aria-label="Количество операций" @change="loadOperations">
          <option v-for="value in [20, 50, 100, 500]" :key="value" :value="value">{{ value }}</option>
        </select>
      </label>
    </div>
    <p v-if="operationsError" class="error">{{ operationsError }}</p>
    <p v-else-if="!operations">Загрузка…</p>
    <template v-else>
      <div v-if="operations.items.length === 0" class="empty">Операции отсутствуют</div>
      <div v-else class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Дата</th><th>Тикер</th><th>Тип</th><th>Статус</th><th>Брокер</th><th>Счёт</th><th>Количество</th><th>Цена</th><th>Сумма</th><th>Комиссия</th></tr></thead>
          <tbody><tr v-for="item in operations.items" :key="item.broker_id + item.operation_id">
            <td>{{ new Date(item.occurred_at).toLocaleString() }}</td><td>{{ item.ticker || "—" }}</td><td>{{ item.operation_type }}</td><td>{{ item.state }}</td><td>{{ item.broker_name }}</td><td>{{ item.account_id }}</td><td>{{ item.quantity }}</td><td>{{ money(item.price) }}</td><td>{{ money(item.payment) }}</td><td>{{ money(item.commission) }}</td>
          </tr></tbody>
        </table>
      </div>
      <p v-for="item in operations.errors" :key="item.code + item.account_id" class="error">{{ item.broker_name }}: {{ item.message }}</p>
    </template>
  </section>
</template>
