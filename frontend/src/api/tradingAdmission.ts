import { apiFetch } from "./request"

export type AdmissionReason =
  | "NOT_OWNER" | "DISARMED" | "STOP_REQUIRED" | "LEASE_EXPIRED" | "CONFIGURATION_CHANGED"
  | "UID_NOT_ALLOWED" | "STALE_MARKET" | "ORDER_NOTIONAL_EXCEEDED" | "EXPOSURE_EXCEEDED"
  | "OUTSTANDING_ORDERS_EXCEEDED" | "DAILY_LOSS_EXCEEDED" | "DATA_UNKNOWN" | "FOREIGN_ACTIVE_ORDER"
  | "INSUFFICIENT_CASH" | "INSUFFICIENT_POSITION" | "BASELINE_REQUIRED" | "RECONCILIATION_REQUIRED"
  | "ALREADY_CONSUMED" | "CORE_UNAVAILABLE"

export type PreparationStatus = "DISABLED" | "WAITING" | "BASELINE_ACCEPTED" | "PREPARED" | "BLOCKED"
export type PreparationReason =
  | "DAILY_PREPARATION_DISABLED" | "NOT_OBSERVED" | "SCOPE_UNAVAILABLE" | "OWNER_MISSING"
  | "OWNER_MISMATCH" | "LEASE_EXPIRED" | "BASELINE_REQUIRED" | "SNAPSHOT_INVALID"
  | "SNAPSHOT_EXPIRED" | "MOSCOW_DAY_CHANGED" | "CONFIGURATION_CHANGED" | "RECONCILIATION_REQUIRED"
  | "RECONCILIATION_EXPIRED" | "RECOVERY_REQUIRED" | "ARM_REQUIRED" | "RISK_DATA_UNKNOWN" | "CORE_UNAVAILABLE"

export interface AdmissionLimits {
  canary_uid: string
  currency: "RUB"
  max_order_notional: string
  exposure_scope: "CANARY_UID_GROSS"
  max_exposure: string
  max_outstanding_orders: number
  max_daily_loss: string
}

export interface AdmissionBaseline {
  baseline_id: string
  broker_id: string
  account_id: string
  moscow_day: string
}

export interface AdmissionReconciliation {
  reconciliation_id: string
  broker_id: string
  account_id: string
  scope: "ACCOUNT" | "CANARY_UID"
  owner_runtime_instance_id: string
  owner_generation: number
  configuration_fingerprint: string
  status: "ACCOUNT_ONLY" | "CANARY_READY" | "DATA_UNKNOWN"
  canary_uid: string | null
  captured_at: string
  valid_until: string
  can_arm: boolean
  reasons: string[]
}

export interface TradingAdmission {
  broker_id: string
  account_id: string
  owner_runtime_instance_id: string | null
  owner_generation: number | null
  recovery_runtime_instance_id: string | null
  admission_status: "DISARMED" | "ARMED" | "STOP_REQUIRED"
  lease_expires_at: string | null
  arm_expires_at: string | null
  limits: AdmissionLimits | null
  baseline: AdmissionBaseline | null
  reconciliation: AdmissionReconciliation | null
  effective_can_submit: boolean
  effective_denial_reasons: string[]
  recovery_only: boolean
}

export interface TradingPreparation {
  moscow_day: string | null
  captured_at: string | null
  status: PreparationStatus
  reason: PreparationReason | null
  baseline_id: string | null
  owner_runtime_instance_id: string | null
  owner_generation: number | null
  configuration_fingerprint: string | null
  reconciliation_id: string | null
  reconciliation_valid_until: string | null
  suggested_limits: AdmissionLimits | null
  risk_reasons: string[]
}

export class TradingAdmissionApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

type ViewPayload = { found: false } | { found: true; value: unknown }

async function readView(url: string, signal: AbortSignal | undefined, message: string): Promise<ViewPayload> {
  const response = await apiFetch(url, signal ? { signal } : undefined)
  if (response.status === 404) return { found: false }
  if (!response.ok) throw new TradingAdmissionApiError(message, response.status)
  return { found: true, value: await response.json() as unknown }
}

type RecordValue = Record<string, unknown>

const preparationReasons = new Set<PreparationReason>([
  "DAILY_PREPARATION_DISABLED", "NOT_OBSERVED", "SCOPE_UNAVAILABLE", "OWNER_MISSING", "OWNER_MISMATCH",
  "LEASE_EXPIRED", "BASELINE_REQUIRED", "SNAPSHOT_INVALID", "SNAPSHOT_EXPIRED", "MOSCOW_DAY_CHANGED",
  "CONFIGURATION_CHANGED", "RECONCILIATION_REQUIRED", "RECONCILIATION_EXPIRED", "RECOVERY_REQUIRED",
  "ARM_REQUIRED", "RISK_DATA_UNKNOWN", "CORE_UNAVAILABLE",
])
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const fingerprintPattern = /^[0-9a-f]{64}$/
const timestampPattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/
const dayPattern = /^\d{4}-\d{2}-\d{2}$/

function record(value: unknown): value is RecordValue {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function requiredString(value: unknown): value is string { return typeof value === "string" && value.length > 0 }
function nullableString(value: unknown): value is string | null { return value === null || requiredString(value) }
function uuid(value: unknown): value is string { return typeof value === "string" && uuidPattern.test(value) }
function nullableUuid(value: unknown): value is string | null { return value === null || uuid(value) }
function positiveInteger(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) > 0 }
function nullablePositiveInteger(value: unknown): value is number | null { return value === null || positiveInteger(value) }
function dateString(value: unknown): value is string {
  if (typeof value !== "string" || !dayPattern.test(value)) return false
  const date = new Date(`${value}T00:00:00.000Z`)
  return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value
}
function nullableDateString(value: unknown): value is string | null { return value === null || dateString(value) }
function timestamp(value: unknown): value is string {
  if (typeof value !== "string" || !timestampPattern.test(value)) return false
  const date = new Date(value)
  return !Number.isNaN(date.valueOf()) && date.toISOString() === value
}
function nullableTimestamp(value: unknown): value is string | null { return value === null || timestamp(value) }
function nullableFingerprint(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && fingerprintPattern.test(value))
}
function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string" && item.length > 0)
}
function positiveNumericString(value: unknown): value is string {
  if (typeof value !== "string" || !/^(?:0|[1-9]\d*)(?:\.\d+)?(?:E[+-]?(?:0|[1-9]\d*))?$/.test(value)) return false
  const numericValue = Number(value)
  return Number.isFinite(numericValue) && numericValue > 0
}

function validLimits(value: unknown): value is AdmissionLimits {
  return record(value) && requiredString(value.canary_uid) && value.currency === "RUB" &&
    positiveNumericString(value.max_order_notional) && value.exposure_scope === "CANARY_UID_GROSS" &&
    positiveNumericString(value.max_exposure) && positiveInteger(value.max_outstanding_orders) &&
    positiveNumericString(value.max_daily_loss)
}

function validBaseline(value: unknown): value is AdmissionBaseline | null {
  if (value === null) return true
  return record(value) && uuid(value.baseline_id) && uuid(value.broker_id) && requiredString(value.account_id) &&
    dateString(value.moscow_day)
}

function validReconciliation(value: unknown): value is AdmissionReconciliation | null {
  if (value === null) return true
  return record(value) && uuid(value.reconciliation_id) && uuid(value.broker_id) && requiredString(value.account_id) &&
    (value.scope === "ACCOUNT" || value.scope === "CANARY_UID") && uuid(value.owner_runtime_instance_id) &&
    positiveInteger(value.owner_generation) && typeof value.configuration_fingerprint === "string" &&
    fingerprintPattern.test(value.configuration_fingerprint) &&
    (value.status === "ACCOUNT_ONLY" || value.status === "CANARY_READY" || value.status === "DATA_UNKNOWN") &&
    nullableString(value.canary_uid) && timestamp(value.captured_at) && timestamp(value.valid_until) &&
    typeof value.can_arm === "boolean" && stringArray(value.reasons)
}

function validAdmission(value: unknown): value is TradingAdmission {
  if (!(record(value) && uuid(value.broker_id) && requiredString(value.account_id) &&
    nullableUuid(value.owner_runtime_instance_id) &&
    nullablePositiveInteger(value.owner_generation) && nullableUuid(value.recovery_runtime_instance_id) &&
    (value.admission_status === "DISARMED" || value.admission_status === "ARMED" || value.admission_status === "STOP_REQUIRED") &&
    nullableTimestamp(value.lease_expires_at) &&
    nullableTimestamp(value.arm_expires_at) && (value.limits === null || validLimits(value.limits)) &&
    validBaseline(value.baseline) && validReconciliation(value.reconciliation) &&
    typeof value.effective_can_submit === "boolean" && stringArray(value.effective_denial_reasons) &&
    typeof value.recovery_only === "boolean" && (value.owner_runtime_instance_id === null) === (value.owner_generation === null))) return false
  if (value.baseline && (value.baseline.broker_id !== value.broker_id || value.baseline.account_id !== value.account_id)) return false
  if (value.reconciliation && (value.reconciliation.broker_id !== value.broker_id || value.reconciliation.account_id !== value.account_id)) return false
  return true
}

function validPreparation(value: unknown): value is TradingPreparation {
  if (!record(value)) return false
  const mayLackObservation = value.status === "DISABLED" || value.status === "WAITING" ||
    (value.status === "BLOCKED" && value.reason === "SCOPE_UNAVAILABLE")
  const observationDateValid = nullableDateString(value.moscow_day) && nullableTimestamp(value.captured_at) &&
    ((value.moscow_day === null) === (value.captured_at === null)) &&
    (mayLackObservation || (dateString(value.moscow_day) && timestamp(value.captured_at)))
  const reasonValid = value.reason === null || (typeof value.reason === "string" && preparationReasons.has(value.reason as PreparationReason))
  const baseShapeValid = observationDateValid &&
    (value.status === "DISABLED" || value.status === "WAITING" || value.status === "BASELINE_ACCEPTED" || value.status === "PREPARED" || value.status === "BLOCKED") &&
    reasonValid && (value.baseline_id === null || uuid(value.baseline_id)) && nullableUuid(value.owner_runtime_instance_id) &&
    nullablePositiveInteger(value.owner_generation) && nullableFingerprint(value.configuration_fingerprint) &&
    nullableUuid(value.reconciliation_id) && nullableTimestamp(value.reconciliation_valid_until) &&
    ((value.reconciliation_id === null) === (value.reconciliation_valid_until === null)) &&
    (value.suggested_limits === null || validLimits(value.suggested_limits)) && stringArray(value.risk_reasons) &&
    ((value.owner_runtime_instance_id === null) === (value.owner_generation === null))
  if (!baseShapeValid) return false
  if (value.status === "DISABLED") return value.reason === "DAILY_PREPARATION_DISABLED"
  if (value.status === "WAITING") return value.reason !== null
  if (value.status === "BASELINE_ACCEPTED") return value.baseline_id !== null && value.owner_runtime_instance_id !== null
  if (value.status === "PREPARED") return value.reason === "ARM_REQUIRED" && value.baseline_id !== null &&
    value.owner_runtime_instance_id !== null && value.configuration_fingerprint !== null &&
    value.reconciliation_id !== null && value.reconciliation_valid_until !== null && value.suggested_limits !== null
  return value.status === "BLOCKED" && value.reason !== null
}

async function fetchView<T>(url: string, signal: AbortSignal | undefined, message: string, validate: (value: unknown) => value is T): Promise<T | null> {
  const payload = await readView(url, signal, message)
  if (!payload.found) return null
  const { value } = payload
  if (!validate(value)) throw new TradingAdmissionApiError(message, 200)
  return value
}

export const fetchTradingAdmission = (brokerId: string, signal?: AbortSignal) => fetchView(
  `/api/brokers/${encodeURIComponent(brokerId)}/trading-admission`, signal, "Не удалось проверить допуск счёта.", validAdmission,
)

export const fetchTradingPreparation = (brokerId: string, signal?: AbortSignal) => fetchView(
  `/api/brokers/${encodeURIComponent(brokerId)}/trading-admission/preparation`, signal, "Не удалось проверить подготовку допуска.", validPreparation,
)
