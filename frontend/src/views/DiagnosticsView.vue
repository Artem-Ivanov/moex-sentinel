<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue"
import frontendPackage from "../../package.json"
import { fetchDiagnosticsStatus, type AwaitingObservation, type DiagnosticsStatus, type ServiceVersionObservation } from "../api/diagnostics"

const snapshot = ref<DiagnosticsStatus>()
const loading = ref(false)
const failed = ref(false)
let controller: AbortController | undefined
let active = true
const observationLabels: Record<AwaitingObservation, string> = {
  worker: "Worker", broker: "Брокер", analytics: "Analytics", market: "Рынок",
  outbox: "Outbox", portfolio: "Портфель", strategy: "Стратегия",
}
const frontendVersion = frontendPackage.version

function serviceVersion(observation?: ServiceVersionObservation): string {
  return observation?.observation === "OBSERVED" && observation.version ? observation.version : "UNKNOWN"
}

async function refresh(): Promise<void> {
  if (loading.value || !active) return
  snapshot.value = undefined
  failed.value = false
  loading.value = true
  controller = new AbortController()
  try {
    const response = await fetchDiagnosticsStatus(controller.signal)
    if (active) snapshot.value = response
  } catch {
    if (active) failed.value = true
  } finally {
    if (active) loading.value = false
  }
}

onMounted(refresh)
onUnmounted(() => { active = false; controller?.abort() })
</script>

<template>
  <section class="page" aria-labelledby="diagnostics-title" :aria-busy="loading">
    <div class="page-title">
      <h2 id="diagnostics-title">Диагностика</h2>
      <button type="button" :disabled="loading" @click="refresh">Обновить</button>
    </div>
    <p>Версия интерфейса: {{ frontendVersion }}</p>
    <p v-if="loading" role="status" aria-live="polite">Загрузка диагностики…</p>
    <p v-if="failed" class="error" role="alert">Не удалось загрузить диагностику. Проверьте соединение и нажмите «Обновить».</p>
    <template v-if="snapshot">
      <p>Серверное время снимка (UTC): <time :datetime="snapshot.captured_at">{{ snapshot.captured_at }}</time></p>
      <article class="grid-row" aria-label="Готовность Core">
        <h3>Общий статус: {{ snapshot.status }}</h3>
        <p>Core: {{ snapshot.core.status }}</p>
        <p>Версия Core: {{ snapshot.core.version }}</p>
        <p>База данных: {{ snapshot.core.database }}</p>
        <p>Схема: {{ snapshot.core.schema }}</p>
        <p>Причина: {{ snapshot.core.reason }}</p>
      </article>
      <article class="grid-row" aria-label="Версии сервисов">
        <h3>Версии сервисов</h3>
        <p>Версия Worker: {{ serviceVersion(snapshot.service_versions?.worker) }}</p>
        <p>Версия Analytics: {{ serviceVersion(snapshot.service_versions?.analytics) }}</p>
      </article>
      <article class="grid-row" aria-label="Runtime Core">
        <h3>Экземпляр Core</h3>
        <p>Среда: {{ snapshot.runtime.environment }}</p>
        <p>Режим доступа Core: {{ snapshot.runtime.access_mode }}</p>
        <p class="hint">Режим доступа не подтверждает фактический режим стратегии.</p>
      </article>
      <article class="grid-row" aria-label="Ожидаемые наблюдения">
        <h3>Наблюдения торговли</h3>
        <p class="hint">Готовность Core не подтверждает готовность торговли. UNKNOWN означает отсутствие наблюдений и не означает остановку сервиса.</p>
        <ul>
          <li v-for="observation in snapshot.awaiting_observations" :key="observation">{{ observationLabels[observation] }}: UNKNOWN</li>
        </ul>
      </article>
    </template>
  </section>
</template>
