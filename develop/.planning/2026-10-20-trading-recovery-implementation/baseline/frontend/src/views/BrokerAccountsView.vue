<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue"
import { useRoute } from "vue-router"
import { fetchBrokerAccounts, type AccountsResponse } from "../api/portfolio"

const route = useRoute()
const data = ref<AccountsResponse>()
const error = ref("")
const money = (value: {amount:string,currency:string}|null) => value ? `${value.amount} ${value.currency}` : "Нет данных"
let generation = 0
let disposed = false

watch(() => String(route.params.brokerId ?? ""), async (brokerId) => {
  const requestGeneration = ++generation
  data.value = undefined
  error.value = ""
  if (disposed || !brokerId) return
  try {
    const response = await fetchBrokerAccounts(brokerId)
    if (!disposed && requestGeneration === generation) data.value = response
  } catch {
    if (!disposed && requestGeneration === generation) error.value = "Не удалось загрузить счета."
  }
}, { immediate: true, flush: "sync" })

onBeforeUnmount(() => { disposed = true })
</script>
<template><section class="page"><p class="eyebrow">Брокер</p><h2>Счета</h2><p v-if="error" class="error">{{ error }}</p><p v-else-if="!data">Загрузка…</p><template v-else><div v-if="data.accounts.length === 0 && data.errors.length === 0" class="empty">Счета отсутствуют</div><article v-for="item in data.accounts" :key="item.account_id" class="grid-row"><strong>{{ item.name || item.account_id }}</strong><p>{{ item.broker_name }} · {{ item.status }}</p><p>Стоимость: {{ money(item.total_amount) }} · Свободно: {{ money(item.free_cash) }}</p></article><p v-for="item in data.errors" :key="item.code + item.account_id" class="error">{{ item.broker_name }}: {{ item.message }}</p></template></section></template>
