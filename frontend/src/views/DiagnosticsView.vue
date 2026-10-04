<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue"
import frontendPackage from "../../package.json"
import { fetchDiagnosticsStatus, type AwaitingObservation, type DiagnosticsStatus, type OutboxDiagnosticReason, type ServiceVersionObservation, type WorkerDiagnosticReason, type WorkerIterationResult } from "../api/diagnostics"

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
const workerReasons: Record<WorkerDiagnosticReason, string> = {
  NOT_OBSERVED: "Наблюдение Worker ещё не получено", STALE: "Наблюдение Worker устарело",
  CONTROL_PROGRESS: "Worker завершил цикл управления", CONTROL_STALLED: "Цикл управления не завершился",
  OUTBOX_BLOCKED: "Очередь событий заблокирована", ITERATION_FAILED: "Цикл завершился ошибкой",
}
const outboxReasons: Record<OutboxDiagnosticReason, string> = {
  NOT_OBSERVED: "Снимок очереди ещё не получен", STALE: "Снимок очереди устарел",
  READ_FAILED: "Не удалось прочитать очередь событий", CLEAR: "Очередь пуста",
  PENDING: "Есть события, ожидающие доставки", FAILED: "Есть события с ошибкой доставки",
  OLD_PENDING: "Есть устаревшие события в очереди",
}
const iterationResults: Record<WorkerIterationResult, string> = {
  COMPLETED: "Выполнен", OUTBOX_BLOCKED: "Заблокирован очередью событий", ERROR: "Завершился ошибкой",
}

function serviceVersion(observation?: ServiceVersionObservation): string {
  return observation?.observation === "OBSERVED" && observation.version ? observation.version : "UNKNOWN"
}

function age(value?: number | null): string {
  return value === null || value === undefined ? "—" : `${(value / 1000).toFixed(1)} с`
}

function count(value?: number | null): string | number {
  return value ?? "—"
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
      <template v-if="snapshot.worker">
        <article class="grid-row" aria-label="Цикл управления Worker">
          <h3>Цикл управления Worker</h3>
          <p>Состояние управления: {{ snapshot.worker.status }}</p>
          <p>Причина: {{ workerReasons[snapshot.worker.reason] }}</p>
          <p>Последнее наблюдение Worker (UTC): <time v-if="snapshot.worker.observation === 'OBSERVED' && snapshot.worker.received_at" :datetime="snapshot.worker.received_at">{{ snapshot.worker.received_at }}</time><span v-else>—</span></p>
          <p>Возраст снимка Worker: {{ age(snapshot.worker.observation === "OBSERVED" ? snapshot.worker.age_ms : null) }}</p>
          <p>Завершено циклов управления: {{ count(snapshot.worker.observation === "OBSERVED" ? snapshot.worker.completed_iterations : null) }}</p>
          <p>Возраст последнего цикла: {{ age(snapshot.worker.observation === "OBSERVED" ? snapshot.worker.completion_age_ms : null) }}</p>
          <p>Последний результат: {{ snapshot.worker.observation === "OBSERVED" && snapshot.worker.last_result ? iterationResults[snapshot.worker.last_result] : "—" }}</p>
          <p v-if="snapshot.worker.observation === 'OBSERVED' && snapshot.worker.error_code">Код ошибки: ошибка выполнения цикла Worker</p>
          <p>Последний цикл завершён (UTC): <time v-if="snapshot.worker.observation === 'OBSERVED' && snapshot.worker.last_completed_at" :datetime="snapshot.worker.last_completed_at">{{ snapshot.worker.last_completed_at }}</time><span v-else>—</span></p>
          <p>Последняя итерация завершилась (UTC): <time v-if="snapshot.worker.observation === 'OBSERVED' && snapshot.worker.last_finished_at" :datetime="snapshot.worker.last_finished_at">{{ snapshot.worker.last_finished_at }}</time><span v-else>—</span></p>
          <p class="hint">Завершение цикла означает обработку команд Worker и не подтверждает готовность торговли, bootstrap или брокера.</p>
        </article>
        <article class="grid-row" aria-label="Доставка событий">
          <h3>Доставка событий</h3>
          <p>Состояние доставки: {{ snapshot.worker.outbox.status }}</p>
          <p>Причина: {{ outboxReasons[snapshot.worker.outbox.reason] }}</p>
          <p>Ожидают доставки: {{ count(snapshot.worker.outbox.observation === "OBSERVED" ? snapshot.worker.outbox.pending_count : null) }}</p>
          <p>Ошибки доставки: {{ count(snapshot.worker.outbox.observation === "OBSERVED" ? snapshot.worker.outbox.failed_count : null) }}</p>
          <p>Старейшее ожидающее событие (UTC): <time v-if="snapshot.worker.outbox.observation === 'OBSERVED' && snapshot.worker.outbox.oldest_pending_at" :datetime="snapshot.worker.outbox.oldest_pending_at">{{ snapshot.worker.outbox.oldest_pending_at }}</time><span v-else>—</span></p>
          <p>Возраст старейшего события: {{ age(snapshot.worker.outbox.observation === "OBSERVED" ? snapshot.worker.outbox.oldest_pending_age_ms : null) }}</p>
          <p>Максимум повторов: {{ count(snapshot.worker.outbox.observation === "OBSERVED" ? snapshot.worker.outbox.max_retry_count : null) }}</p>
        </article>
      </template>
      <p v-else class="hint">Наблюдение Worker отсутствует в этом ответе</p>
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
