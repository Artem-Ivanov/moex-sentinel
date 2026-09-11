<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue"

import { fetchTradingSessionsStatus, type TradingSessionsStatus } from "../api/automations"

const state = ref<TradingSessionsStatus>()
let timer: ReturnType<typeof setInterval> | undefined

async function refresh(): Promise<void> {
  try { state.value = await fetchTradingSessionsStatus() }
  catch { state.value = { status: "UNAVAILABLE", total: 0, open: 0, closed: 0, unavailable: 0 } }
}

onMounted(async () => { await refresh(); timer = setInterval(refresh, 60_000) })
onUnmounted(() => { if (timer !== undefined) clearInterval(timer) })
</script>

<template>
  <div class="health-status" :data-status="state?.status === 'OPEN' ? 'online' : 'offline'" role="status" aria-live="polite">
    <span class="health-status__dot" aria-hidden="true" />
    <span v-if="!state">Проверка торгов…</span>
    <span v-else-if="state.status === 'NO_ACTIVE'">Нет активных инструментов</span>
    <span v-else-if="state.status === 'OPEN'">Торги доступны: {{ state.open }}/{{ state.total }}</span>
    <span v-else-if="state.status === 'CLOSED'">Торговая сессия закрыта</span>
    <span v-else>Статус торгов недоступен</span>
  </div>
</template>
