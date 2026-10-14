<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue"
import {
  fetchTradingAdmission,
  fetchTradingPreparation,
  TradingAdmissionApiError,
  type AdmissionReason,
  type PreparationReason,
  type TradingAdmission,
  type TradingPreparation,
} from "../api/tradingAdmission"

const props = defineProps<{ brokerId: string; accountId: string; positionState: string; resumeRequested: boolean }>()

const POLL_INTERVAL_MS = 15_000
const VIEW_FRESHNESS_MS = 15_000
const admission = ref<TradingAdmission | null>()
const preparation = ref<TradingPreparation | null>()
const admissionError = ref("")
const preparationError = ref("")
const admissionLoadedAt = ref<number>()
const preparationLoadedAt = ref<number>()
const now = ref(Date.now())
const loading = ref(false)
let generation = 0
let controller: AbortController | undefined
let pollTimer: ReturnType<typeof setInterval> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
let disposed = false

function moscowDay(timestamp: number): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(timestamp)
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]))
  return `${values.year}-${values.month}-${values.day}`
}

function dateTime(value: string | null | undefined): string {
  if (!value) return "не указано"
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return "время неизвестно"
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow", dateStyle: "short", timeStyle: "medium",
  }).format(date)
}

function isExpired(value: string | null | undefined): boolean {
  if (!value) return true
  const timestamp = Date.parse(value)
  return !Number.isFinite(timestamp) || timestamp <= now.value
}

const reasonText: Record<string, string> = {
  NOT_OWNER: "Runtime не подтверждён как владелец. Оператор должен проверить owner и передачу.",
  DISARMED: "Допуск не активирован. Требуется явное действие оператора после проверки условий.",
  STOP_REQUIRED: "Управление остановкой требуется; продолжите восстановление через оператора.",
  LEASE_EXPIRED: "Lease владельца истёк. Оператор должен проверить владельца и восстановление.",
  CONFIGURATION_CHANGED: "Конфигурация изменилась. Требуется проверка конфигурации и повторная подготовка.",
  UID_NOT_ALLOWED: "UID отсутствует в разрешённом списке. Оператор должен проверить выбранный инструмент.",
  STALE_MARKET: "Риск/рынок: цена устарела.",
  ORDER_NOTIONAL_EXCEEDED: "Размер заявки превышает лимит. Проверьте лимит и параметры заявки.",
  EXPOSURE_EXCEEDED: "Превышен лимит риска по позиции. Требуется проверка риска оператором.",
  OUTSTANDING_ORDERS_EXCEEDED: "Превышен лимит незавершённых заявок. Сверьте состояние у оператора.",
  DAILY_LOSS_EXCEEDED: "Достигнут дневной лимит убытка. Требуется проверка оператора.",
  DATA_UNKNOWN: "Данные риска неизвестны. Необходима повторная проверка источников.",
  FOREIGN_ACTIVE_ORDER: "Найдена активная заявка вне подтверждённой сверки. Оператор должен её проверить.",
  INSUFFICIENT_CASH: "Недостаточно подтверждённых свободных средств для допуска.",
  INSUFFICIENT_POSITION: "Недостаточно подтверждённой позиции для допуска.",
  BASELINE_REQUIRED: "Нет принятой базы текущего московского дня. Оператор должен подготовить базу.",
  RECONCILIATION_REQUIRED: "Нет свежей подтверждённой сверки. Оператор должен выполнить сверку.",
  ALREADY_CONSUMED: "Результат заявки требует read-only сверки; повторную команду не отправлять.",
  CORE_UNAVAILABLE: "Core недоступен. Статус допуска неизвестен.",
  DAILY_PREPARATION_DISABLED: "Ежедневная подготовка отключена; используйте только согласованный операторский сценарий.",
  NOT_OBSERVED: "Нет наблюдения подготовки. Дождитесь проверки Core.",
  SCOPE_UNAVAILABLE: "Допуск для этого счёта недоступен. Оператор должен проверить счёт.",
  OWNER_MISSING: "Runtime-владелец не зарегистрирован. Оператор должен проверить Worker.",
  OWNER_MISMATCH: "Runtime не совпадает с владельцем счёта. Нужна контролируемая передача.",
  SNAPSHOT_INVALID: "Снимок счёта непригоден. Оператор должен проверить данные брокера.",
  SNAPSHOT_EXPIRED: "Снимок счёта устарел. Требуется новая проверка.",
  MOSCOW_DAY_CHANGED: "Начался новый московский день. Дождитесь новой подготовки.",
  RECONCILIATION_EXPIRED: "Срок сверки истёк. Оператор должен выполнить новую сверку.",
  RECOVERY_REQUIRED: "Нужна явная сверка/восстановление допуска оператором; остановка сохраняется.",
  ARM_REQUIRED: "Подготовка завершена; ARM остаётся отдельным явным действием оператора.",
  RISK_DATA_UNKNOWN: "Риск-данные неизвестны или устарели. Требуется проверка рынка оператором.",
}

function explainReason(reason: AdmissionReason | PreparationReason | string): string {
  return reasonText[reason] ?? "Неизвестная причина блокировки. Считайте статус неизвестным и передайте оператору."
}

const accountViewFresh = computed(() => admissionLoadedAt.value !== undefined && now.value - admissionLoadedAt.value < VIEW_FRESHNESS_MS)
const preparationViewFresh = computed(() => preparationLoadedAt.value !== undefined && now.value - preparationLoadedAt.value < VIEW_FRESHNESS_MS)
const currentMoscowDay = computed(() => moscowDay(now.value))
const baselineForToday = computed(() => admission.value?.baseline?.moscow_day === currentMoscowDay.value)
const leaseValid = computed(() => Boolean(admission.value?.lease_expires_at && Date.parse(admission.value.lease_expires_at) > now.value))
const armValid = computed(() => Boolean(admission.value?.arm_expires_at && Date.parse(admission.value.arm_expires_at) > now.value))
const accountAllowed = computed(() => Boolean(
  !loading.value && admission.value?.effective_can_submit === true && admission.value.admission_status === "ARMED" &&
  admission.value.effective_denial_reasons.length === 0 && !admission.value.recovery_only &&
  admission.value.owner_runtime_instance_id && admission.value.owner_generation !== null && admission.value.limits &&
  accountViewFresh.value && leaseValid.value && armValid.value && baselineForToday.value,
))
const admissionReasonKeys = computed(() => {
  const reasons = new Set<string>(admission.value?.effective_denial_reasons ?? [])
  if (admission.value?.recovery_only) reasons.add("STOP_REQUIRED")
  if (admission.value?.owner_runtime_instance_id && !leaseValid.value) reasons.add("LEASE_EXPIRED")
  if (admission.value?.admission_status === "ARMED" && !armValid.value) reasons.add("DISARMED")
  if (!baselineForToday.value) reasons.add("BASELINE_REQUIRED")
  return [...reasons]
})
const riskReasonKeys = computed(() => [...new Set([
  ...(admission.value?.reconciliation?.reasons ?? []),
  ...(preparation.value?.risk_reasons ?? []),
])])
const preparationExpired = computed(() => Boolean(
  preparation.value?.status === "PREPARED" &&
  isExpired(preparation.value.reconciliation_valid_until),
))
const preparationCaptureCurrent = computed(() => {
  const capturedAt = preparation.value?.captured_at
  if (!capturedAt) return false
  const capturedMs = Date.parse(capturedAt)
  return Number.isFinite(capturedMs) && capturedMs <= now.value && now.value <= capturedMs + 30_000
})
const preparationDayChanged = computed(() => Boolean(
  preparation.value?.moscow_day && preparation.value.moscow_day !== currentMoscowDay.value,
))
const preparationSnapshotStale = computed(() => Boolean(
  preparation.value?.status === "PREPARED" && !preparationViewFresh.value,
))
const preparationAdmissionCompatible = computed(() => {
  const currentAdmission = admission.value
  const currentPreparation = preparation.value
  const reconciliation = currentAdmission?.reconciliation
  const suggestedLimits = currentPreparation?.suggested_limits
  return Boolean(currentAdmission && currentPreparation && reconciliation && suggestedLimits &&
    currentAdmission.broker_id === props.brokerId && currentAdmission.account_id === props.accountId &&
    currentAdmission.admission_status === "DISARMED" && !currentAdmission.recovery_only &&
    !currentAdmission.effective_can_submit && currentAdmission.owner_runtime_instance_id &&
    currentAdmission.owner_generation !== null && currentAdmission.baseline && baselineForToday.value &&
    currentAdmission.baseline.baseline_id === currentPreparation.baseline_id &&
    currentAdmission.owner_runtime_instance_id === currentPreparation.owner_runtime_instance_id &&
    currentAdmission.owner_generation === currentPreparation.owner_generation &&
    leaseValid.value && accountViewFresh.value &&
    reconciliation.reconciliation_id === currentPreparation.reconciliation_id &&
    reconciliation.scope === "CANARY_UID" && reconciliation.status === "CANARY_READY" && reconciliation.can_arm &&
    reconciliation.reasons.length === 0 && currentPreparation.risk_reasons.length === 0 &&
    reconciliation.owner_runtime_instance_id === currentPreparation.owner_runtime_instance_id &&
    reconciliation.owner_generation === currentPreparation.owner_generation &&
    reconciliation.configuration_fingerprint === currentPreparation.configuration_fingerprint &&
    reconciliation.canary_uid === suggestedLimits.canary_uid &&
    reconciliation.valid_until === currentPreparation.reconciliation_valid_until &&
    Date.parse(reconciliation.captured_at) <= now.value && !isExpired(reconciliation.valid_until))
})
const preparationReadyForArm = computed(() => Boolean(
  preparation.value?.status === "PREPARED" && preparation.value.reason === "ARM_REQUIRED" &&
  !loading.value && preparationViewFresh.value && preparationCaptureCurrent.value &&
  preparation.value.moscow_day === currentMoscowDay.value && preparationAdmissionCompatible.value,
))

function isCurrent(requestGeneration: number, brokerId: string): boolean {
  return !disposed && requestGeneration === generation && props.brokerId === brokerId
}

async function refresh(): Promise<void> {
  const brokerId = props.brokerId
  const accountId = props.accountId
  if (disposed || !brokerId || !accountId) return
  controller?.abort()
  const requestController = new AbortController()
  controller = requestController
  const requestGeneration = ++generation
  loading.value = true
  const results = await Promise.allSettled([
    fetchTradingAdmission(brokerId, requestController.signal),
    fetchTradingPreparation(brokerId, requestController.signal),
  ])
  if (!isCurrent(requestGeneration, brokerId)) return
  const [admissionResult, preparationResult] = results
  if (admissionResult.status === "fulfilled") {
    const value = admissionResult.value
    if (value && (value.broker_id !== brokerId || value.account_id !== accountId)) {
      admission.value = undefined
      admissionLoadedAt.value = undefined
      admissionError.value = "Статус допуска неизвестен: ответ относится к другому счёту."
    } else {
      admission.value = value
      admissionLoadedAt.value = Date.now()
      admissionError.value = ""
    }
  } else {
    admission.value = undefined
    admissionLoadedAt.value = undefined
    admissionError.value = admissionResult.reason instanceof TradingAdmissionApiError && admissionResult.reason.status >= 500
      ? "Статус допуска неизвестен: Core недоступен."
      : "Статус допуска неизвестен: не удалось проверить Core."
  }
  if (preparationResult.status === "fulfilled") {
    preparation.value = preparationResult.value
    preparationLoadedAt.value = Date.now()
    preparationError.value = ""
  } else {
    preparation.value = undefined
    preparationLoadedAt.value = undefined
    preparationError.value = "Не удалось проверить ежедневную подготовку; её состояние неизвестно."
  }
  loading.value = false
}

watch(() => [props.brokerId, props.accountId] as const, ([brokerId, accountId]) => {
  generation += 1
  controller?.abort()
  admission.value = undefined
  preparation.value = undefined
  admissionLoadedAt.value = undefined
  preparationLoadedAt.value = undefined
  admissionError.value = ""
  preparationError.value = ""
  loading.value = false
  if (pollTimer !== undefined) clearInterval(pollTimer)
  if (!brokerId || !accountId || disposed) return
  void refresh()
  pollTimer = setInterval(() => { void refresh() }, POLL_INTERVAL_MS)
}, { immediate: true, flush: "sync" })

clockTimer = setInterval(() => { now.value = Date.now() }, 1000)
onBeforeUnmount(() => {
  disposed = true
  generation += 1
  controller?.abort()
  if (pollTimer !== undefined) clearInterval(pollTimer)
  if (clockTimer !== undefined) clearInterval(clockTimer)
})
</script>

<template>
  <article class="candle-panel" aria-label="Состояние позиции и допуска счёта">
    <div class="candle-panel__header"><strong>Состояние позиции и допуск счёта</strong></div>
    <div class="detail-grid">
      <section class="grid-row" aria-label="Состояние позиции">
        <strong>Позиция</strong>
        <p>Состояние: <span class="status-pill">{{ positionState }}</span></p>
        <p v-if="resumeRequested" role="status">Запрос Resume принят.</p>
      </section>
      <section class="grid-row" aria-label="Допуск счёта">
        <strong>Счёт: {{ accountId }}</strong>
        <p v-if="admissionError" class="error" role="status">{{ admissionError }}</p>
        <p v-else-if="admission === undefined">{{ loading ? "Загрузка допуска…" : "Статус допуска неизвестен." }}</p>
        <template v-else-if="admission === null">
          <p>Состояние допуска не зарегистрировано для счёта.</p>
          <p>Требуется подготовка у оператора; интерфейс не создаёт admission.</p>
        </template>
        <template v-else-if="!accountViewFresh">
          <p class="error" role="status">Снимок допуска устарел; статус неизвестен до ответа Core.</p>
        </template>
        <template v-else>
          <p>Статус Core: <span class="status-pill">{{ admission.admission_status }}</span></p>
          <p v-if="loading" class="hint" role="status">Проверяется свежий статус Core; предыдущий снимок не подтверждает доступность.</p>
          <p v-else-if="accountAllowed" role="status">Допуск счёта подтверждён последним снимком Core.</p>
          <p v-else-if="admissionReasonKeys.length === 0">Допуск не подтверждён; требуется проверка оператора.</p>
          <ul v-else class="hint">
            <li v-for="reason in admissionReasonKeys" :key="reason"><strong>{{ reason }}:</strong> {{ explainReason(reason) }}</li>
          </ul>
          <p>Московская дата базы: {{ admission.baseline?.moscow_day ?? "нет принятой базы" }}</p>
          <p>Владелец: {{ admission.owner_runtime_instance_id ?? "не подтверждён" }} · поколение {{ admission.owner_generation ?? "—" }}</p>
          <p>Lease до: {{ dateTime(admission.lease_expires_at) }}</p>
          <p>ARM до: {{ dateTime(admission.arm_expires_at) }}</p>
          <p v-if="admission.limits">Лимиты: заявка {{ admission.limits.max_order_notional }} {{ admission.limits.currency }}; риск {{ admission.limits.max_exposure }} {{ admission.limits.currency }}; незавершённые {{ admission.limits.max_outstanding_orders }}; дневной убыток {{ admission.limits.max_daily_loss }} {{ admission.limits.currency }}.</p>
          <template v-if="admission.reconciliation">
            <p>Сверка {{ admission.reconciliation.scope }} / {{ admission.reconciliation.status }} · получена {{ dateTime(admission.reconciliation.captured_at) }}.</p>
            <p :class="isExpired(admission.reconciliation.valid_until) ? 'error' : 'hint'">
              Сверка {{ isExpired(admission.reconciliation.valid_until) ? "истекла или имеет некорректное время" : "действительна до" }} {{ dateTime(admission.reconciliation.valid_until) }}.
            </p>
          </template>
          <p v-else>Подтверждённой сверки нет.</p>
        </template>
      </section>
    </div>
    <p class="hint">Режим TRADE и эта сводка не подтверждают свежую цену или доступность брокера для заявки.</p>
    <section aria-label="Подготовка к явному ARM">
      <strong>Ежедневная подготовка</strong>
      <p v-if="preparationError" class="error" role="status">{{ preparationError }}</p>
      <p v-else-if="preparation === undefined">{{ loading ? "Загрузка подготовки…" : "Подготовка неизвестна." }}</p>
      <template v-else-if="preparation === null">
        <p>Ежедневная подготовка не опубликована или отключена.</p>
      </template>
      <template v-else>
        <p>День {{ preparation.moscow_day }} · состояние {{ preparation.status }} · снимок {{ dateTime(preparation.captured_at) }}.</p>
        <p v-if="preparationReadyForArm" role="status">Готово только к явному ARM оператора.</p>
        <p v-else-if="preparationExpired" class="error" role="status">Срок подготовки истёк; требуется новая сверка перед ARM.</p>
        <p v-else-if="preparationDayChanged" class="error" role="status">Подготовка относится к другому московскому дню; требуется новая проверка.</p>
        <p v-else-if="preparationSnapshotStale" class="error" role="status">Снимок подготовки устарел; требуется повторная проверка перед ARM.</p>
        <p v-else-if="loading" class="hint" role="status">Проверяется свежая подготовка; предыдущий снимок не подтверждает готовность к ARM.</p>
        <p v-else-if="preparation.status === 'PREPARED' && !preparationCaptureCurrent" class="error" role="status">Время снимка не подтверждает подготовку в допустимом 30-секундном окне.</p>
        <p v-else-if="preparation.status === 'PREPARED' && !preparationAdmissionCompatible" class="error" role="status">Подготовка не совпадает с актуальным допуском счёта; готовность к ARM не подтверждена.</p>
        <p v-else-if="preparation.reason"><strong>{{ preparation.reason }}:</strong> {{ explainReason(preparation.reason) }}</p>
        <p v-if="preparation.suggested_limits">Предварительные лимиты для {{ preparation.suggested_limits.canary_uid }}: <span>{{ preparation.suggested_limits.max_order_notional }} {{ preparation.suggested_limits.currency }}</span>.</p>
        <p v-if="preparation.suggested_limits" class="hint">Это preview лимитов; ARM и возобновление позиции не выполняются интерфейсом.</p>
      </template>
    </section>
    <p v-for="reason in riskReasonKeys" :key="reason" class="hint">{{ explainReason(reason) }}</p>
    <button type="button" :disabled="loading" @click="refresh">{{ loading ? "Обновление…" : "Обновить допуск" }}</button>
  </article>
</template>
