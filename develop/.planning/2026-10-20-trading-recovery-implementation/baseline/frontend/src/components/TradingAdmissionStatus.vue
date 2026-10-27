<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue"
import {
  fetchTradingAdmission,
  TradingAdmissionApiError,
  updateTradingAdmissionLimits,
  type AdmissionLimits,
  type TradingAdmission,
} from "../api/tradingAdmission"

const props = defineProps<{ brokerId: string; accountId: string; positionState: string; resumeRequested: boolean }>()

const POLL_INTERVAL_MS = 15_000
const VIEW_FRESHNESS_MS = 15_000
const admission = ref<TradingAdmission | null>()
const admissionError = ref("")
const admissionLoadedAt = ref<number>()
const now = ref(Date.now())
const loading = ref(false)
const saving = ref(false)
const saveError = ref("")
const saveMessage = ref("")
const formDirty = ref(false)
const formBaseRevision = ref<number>()
const revisionConflict = ref(false)
const maxOrderNotional = ref("")
const maxExposure = ref("")
const maxOutstandingOrders = ref("")
const maxDailyLoss = ref("")
let generation = 0
let controller: AbortController | undefined
let pollTimer: ReturnType<typeof setInterval> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
let disposed = false

const accountViewFresh = computed(() => admissionLoadedAt.value !== undefined && now.value - admissionLoadedAt.value < VIEW_FRESHNESS_MS)
const leaseValid = computed(() => Boolean(admission.value?.lease_expires_at && Date.parse(admission.value.lease_expires_at) > now.value))
const currentOwner = computed(() => Boolean(admission.value?.owner_runtime_instance_id && admission.value.owner_generation !== null))
const viewMatchesAccount = computed(() => admission.value?.broker_id === props.brokerId && admission.value.account_id === props.accountId)

const reasonText: Record<string, string> = {
  NOT_OWNER: "Runtime не является текущим владельцем счёта.",
  LEASE_EXPIRED: "Lease владельца истёк.",
  CONFIGURATION_CHANGED: "Конфигурация runtime не совпадает с подтверждённой конфигурацией Core.",
  STALE_MARKET: "Рыночные данные устарели.",
  ORDER_NOTIONAL_EXCEEDED: "Заявка превышает лимит размера.",
  EXPOSURE_EXCEEDED: "Превышен лимит общей экспозиции счёта.",
  OUTSTANDING_ORDERS_EXCEEDED: "Превышен лимит незавершённых заявок.",
  DAILY_LOSS_EXCEEDED: "Достигнут лимит дневного убытка.",
  DATA_UNKNOWN: "Состояние риска неизвестно.",
  FOREIGN_ACTIVE_ORDER: "Обнаружена активная заявка, не учтённая в состоянии счёта.",
  INSUFFICIENT_CASH: "Недостаточно подтверждённых свободных средств.",
  INSUFFICIENT_POSITION: "Недостаточно подтверждённой позиции.",
  LIMITS_NOT_CONFIGURED: "Лимиты счёта не настроены.",
  AUTOMATION_NOT_ACTIVE: "Автоматизация не находится в состоянии IN_WORK.",
  BASELINE_REQUIRED: "Core автоматически подготавливает дневную базу счёта.",
  CORE_UNAVAILABLE: "Core недоступен; состояние допуска неизвестно.",
  HOLD: "Позиция остаётся в HOLD.",
}

function explainReason(reason: string): string {
  return reasonText[reason] ?? "Причина не распознана; состояние допуска неизвестно."
}

function seedForm(value: TradingAdmission | null | undefined): void {
  if (formDirty.value) return
  if (!value?.limits) {
    formBaseRevision.value = value?.revision
    revisionConflict.value = false
    maxOrderNotional.value = ""
    maxExposure.value = ""
    maxOutstandingOrders.value = ""
    maxDailyLoss.value = ""
    return
  }
  formBaseRevision.value = value.revision
  revisionConflict.value = false
  maxOrderNotional.value = value.limits.max_order_notional
  maxExposure.value = value.limits.max_exposure
  maxOutstandingOrders.value = String(value.limits.max_outstanding_orders)
  maxDailyLoss.value = value.limits.max_daily_loss
}

function isPositiveDecimal(value: string): boolean {
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?(?:E[+-]?(?:0|[1-9]\d*))?$/i.test(value.trim())) return false
  const numeric = Number(value)
  return Number.isFinite(numeric) && numeric > 0
}

const validForm = computed(() => isPositiveDecimal(maxOrderNotional.value) &&
  isPositiveDecimal(maxExposure.value) && isPositiveDecimal(maxDailyLoss.value) &&
  /^[1-9]\d*$/.test(maxOutstandingOrders.value) && Number.isSafeInteger(Number(maxOutstandingOrders.value)))

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
  try {
    const value = await fetchTradingAdmission(brokerId, requestController.signal)
    if (!isCurrent(requestGeneration, brokerId)) return
    if (value && (value.broker_id !== brokerId || value.account_id !== accountId)) {
      admission.value = undefined
      admissionLoadedAt.value = undefined
      admissionError.value = "Статус допуска неизвестен: ответ относится к другому счёту."
    } else {
      if (formDirty.value && value && formBaseRevision.value !== undefined && value.revision !== formBaseRevision.value) {
        revisionConflict.value = true
      }
      admission.value = value
      admissionLoadedAt.value = Date.now()
      admissionError.value = ""
      seedForm(value)
    }
  } catch (error) {
    if (!isCurrent(requestGeneration, brokerId)) return
    admission.value = undefined
    admissionLoadedAt.value = undefined
    admissionError.value = error instanceof TradingAdmissionApiError && error.status >= 500
      ? "Статус допуска неизвестен: Core недоступен."
      : "Статус допуска неизвестен: не удалось проверить Core."
  } finally {
    if (isCurrent(requestGeneration, brokerId)) loading.value = false
  }
}

async function saveLimits(): Promise<void> {
  const current = admission.value
  if (!current || current.broker_id !== props.brokerId || current.account_id !== props.accountId ||
      formBaseRevision.value === undefined || revisionConflict.value || !validForm.value || saving.value) return
  saving.value = true
  saveError.value = ""
  saveMessage.value = ""
  const limits: AdmissionLimits = {
    currency: "RUB",
    exposure_scope: "ACCOUNT_GROSS",
    max_order_notional: maxOrderNotional.value.trim(),
    max_exposure: maxExposure.value.trim(),
    max_outstanding_orders: Number(maxOutstandingOrders.value),
    max_daily_loss: maxDailyLoss.value.trim(),
  }
  try {
    const updated = await updateTradingAdmissionLimits(props.brokerId, props.accountId, formBaseRevision.value, limits)
    if (updated.account_id !== props.accountId) throw new TradingAdmissionApiError("Не удалось сохранить лимиты счёта.", 200)
    admission.value = updated
    admissionLoadedAt.value = Date.now()
    formDirty.value = false
    revisionConflict.value = false
    seedForm(updated)
    saveMessage.value = "Лимиты счёта сохранены в Core."
  } catch (error) {
    const conflict = error instanceof TradingAdmissionApiError && error.status === 409
    if (conflict) revisionConflict.value = true
    saveError.value = conflict
      ? "Лимиты изменились в Core. Обновляю статус; проверьте форму и повторите сохранение."
      : "Не удалось сохранить лимиты счёта; ответ Core не подтверждён."
    if (conflict) void refresh()
  } finally {
    saving.value = false
  }
}

function loadLatestLimits(): void {
  if (!admission.value || admission.value.broker_id !== props.brokerId || admission.value.account_id !== props.accountId) return
  formDirty.value = false
  revisionConflict.value = false
  seedForm(admission.value)
  saveError.value = ""
  saveMessage.value = "Загружены актуальные лимиты Core; черновик заменён."
}

watch(() => [props.brokerId, props.accountId] as const, ([brokerId]) => {
  generation += 1
  controller?.abort()
  admission.value = undefined
  admissionLoadedAt.value = undefined
  admissionError.value = ""
  saveError.value = ""
  saveMessage.value = ""
  formDirty.value = false
  formBaseRevision.value = undefined
  revisionConflict.value = false
  loading.value = false
  if (pollTimer !== undefined) clearInterval(pollTimer)
  if (!brokerId || disposed) return
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
  <article class="candle-panel" aria-label="Состояние позиции и лимиты счёта">
    <div class="candle-panel__header"><strong>Состояние позиции и лимиты счёта</strong></div>
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
          <p>Лимиты и состояние автоматизации задаются через Core API.</p>
        </template>
        <template v-else-if="!accountViewFresh || !viewMatchesAccount">
          <p class="error" role="status">Снимок допуска устарел или относится к другому счёту; состояние неизвестно.</p>
        </template>
        <template v-else>
          <p v-if="loading" class="hint" role="status">Проверяется свежий статус Core.</p>
          <p v-if="!leaseValid" class="error" role="status">Lease владельца истёк; допуска нет.</p>
          <p v-else-if="!currentOwner" class="error" role="status">Текущий владелец и generation не подтверждены.</p>
          <p v-else-if="!loading && admission.effective_can_submit && admission.effective_denial_reasons.length === 0" role="status">
            Технические проверки Core сейчас пройдены; доступность конкретной заявки проверяется при её отправке.
          </p>
          <ul v-if="admission.effective_denial_reasons.length > 0" class="hint">
            <li v-for="reason in admission.effective_denial_reasons" :key="reason"><strong>{{ reason }}:</strong> {{ explainReason(reason) }}</li>
          </ul>
          <p v-if="!admission.effective_can_submit && admission.effective_denial_reasons.length === 0" class="hint">Core не подтвердил техническую готовность; причина неизвестна.</p>
          <p>Владелец: {{ admission.owner_runtime_instance_id ?? "не подтверждён" }} · поколение {{ admission.owner_generation ?? "—" }}</p>
          <p>Lease до: {{ admission.lease_expires_at ?? "не указано" }}</p>
        </template>
      </section>
    </div>
    <form aria-label="Лимиты счёта" @submit.prevent="saveLimits" @input="formDirty = true; saveMessage = ''">
      <strong>Общие лимиты счёта, RUB</strong>
      <p class="hint">Лимиты действуют на все инструменты. Их увеличение может снять блокер у уже активных автоматов IN_WORK, после чего подходящие заявки могут отправляться по обычным правилам. Проверки заявки, рынка и риска остаются обязательными.</p>
      <p v-if="revisionConflict" class="error" role="alert">
        Лимиты в Core изменились после открытия формы. Черновик сохранён, но привязан к revision {{ formBaseRevision }};
        актуальная revision {{ admission?.revision ?? "неизвестна" }}. Сохранение остановлено.
      </p>
      <label>Максимальный размер заявки, RUB
        <input v-model="maxOrderNotional" inputmode="decimal" autocomplete="off" :disabled="saving" />
      </label>
      <label>Максимальная экспозиция счёта, RUB
        <input v-model="maxExposure" inputmode="decimal" autocomplete="off" :disabled="saving" />
      </label>
      <label>Максимум незавершённых заявок
        <input v-model="maxOutstandingOrders" inputmode="numeric" autocomplete="off" :disabled="saving" />
      </label>
      <label>Максимальный дневной убыток, RUB
        <input v-model="maxDailyLoss" inputmode="decimal" autocomplete="off" :disabled="saving" />
      </label>
      <button type="submit" :disabled="!admission || !validForm || saving || loading || revisionConflict || !accountViewFresh || !viewMatchesAccount">
        {{ saving ? "Сохранение…" : "Сохранить лимиты счёта" }}
      </button>
      <button v-if="revisionConflict && admission" type="button" :disabled="loading || !viewMatchesAccount" @click="loadLatestLimits">
        Загрузить актуальные лимиты
      </button>
      <p v-if="saveError" class="error" role="status">{{ saveError }}</p>
      <p v-else-if="saveMessage" role="status">{{ saveMessage }}</p>
      <p v-else-if="!validForm" class="error" role="status">Укажите положительные конечные значения; число незавершённых заявок должно быть целым.</p>
    </form>
  </article>
</template>
