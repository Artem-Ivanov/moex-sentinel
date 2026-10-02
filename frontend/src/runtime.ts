import { computed, ref } from "vue"
import { apiFetch } from "./api/request"

export interface Runtime { environment: "TEST" | "PROD"; access_mode: "READ_ONLY" | "TRADE" }
export const runtime = ref<Runtime | null>(null)
export const tradingAllowed = computed(() => runtime.value?.access_mode === "TRADE" && runtime.value.environment === "TEST")
export const tradingDisabledReason = computed(() => runtime.value?.access_mode === "READ_ONLY"
  ? "Только чтение: торговые команды запрещены сервером."
  : "Торговые команды недоступны до подтверждения режима сервера.")

export async function loadRuntime(): Promise<void> {
  runtime.value = null
  try {
    const response = await apiFetch("/api/runtime")
    if (!response.ok) return
    const value = await response.json() as Runtime
    if (["TEST", "PROD"].includes(value.environment) && ["READ_ONLY", "TRADE"].includes(value.access_mode)) runtime.value = value
  } catch { runtime.value = null }
}
