import { apiFetch } from "./request"

export type AwaitingObservation = "worker" | "broker" | "analytics" | "market" | "outbox" | "portfolio" | "strategy"
export type ServiceVersionReason = "OBSERVED" | "NOT_OBSERVED" | "STALE" | "NOT_CONFIGURED" | "UNAVAILABLE" | "INVALID_RESPONSE"

export interface ServiceVersionObservation<Reason extends ServiceVersionReason = ServiceVersionReason> {
  observation: "OBSERVED" | "UNKNOWN"
  version: string | null
  received_at: string | null
  age_ms: number | null
  reason: Reason
}

export type WorkerDiagnosticReason = "NOT_OBSERVED" | "STALE" | "CONTROL_PROGRESS" | "CONTROL_STALLED" | "OUTBOX_BLOCKED" | "ITERATION_FAILED"
export type OutboxDiagnosticReason = "NOT_OBSERVED" | "STALE" | "READ_FAILED" | "CLEAR" | "PENDING" | "FAILED" | "OLD_PENDING"
export type WorkerIterationResult = "COMPLETED" | "OUTBOX_BLOCKED" | "ERROR"

export interface OutboxDiagnosticsObservation {
  observation: "OBSERVED" | "UNKNOWN"
  status: "OK" | "DEGRADED" | "UNKNOWN"
  reason: OutboxDiagnosticReason
  pending_count: number | null
  failed_count: number | null
  oldest_pending_at: string | null
  oldest_pending_age_ms: number | null
  max_retry_count: number | null
}

export interface WorkerDiagnosticsObservation {
  observation: "OBSERVED" | "UNKNOWN"
  status: "OK" | "DEGRADED" | "UNKNOWN"
  reason: WorkerDiagnosticReason
  instance_id: string | null
  received_at: string | null
  age_ms: number | null
  completed_iterations: number | null
  last_completed_at: string | null
  completion_age_ms: number | null
  last_finished_at: string | null
  last_result: WorkerIterationResult | null
  error_code: "ITERATION_FAILED" | null
  outbox: OutboxDiagnosticsObservation
}

export interface DiagnosticsStatus {
  captured_at: string
  status: "UNKNOWN" | "DOWN" | "DEGRADED"
  core: {
    status: "OK" | "DOWN" | "DEGRADED"
    version: string
    database: "ok" | "error"
    schema: "compatible" | "unknown" | "not_ready"
    reason: "READY" | "DATABASE_UNAVAILABLE" | "SCHEMA_NOT_READY"
  }
  runtime: { environment: "TEST" | "PROD"; access_mode: "READ_ONLY" | "TRADE" }
  service_versions?: {
    worker: ServiceVersionObservation<"OBSERVED" | "NOT_OBSERVED" | "STALE">
    analytics: ServiceVersionObservation<"OBSERVED" | "NOT_CONFIGURED" | "UNAVAILABLE" | "INVALID_RESPONSE">
  }
  /** Absent in responses from earlier Core versions. */
  worker?: WorkerDiagnosticsObservation | null
  awaiting_observations: AwaitingObservation[]
}

export async function fetchDiagnosticsStatus(signal?: AbortSignal): Promise<DiagnosticsStatus> {
  const response = await apiFetch("/api/diagnostics/status", { signal })
  if (!response.ok) throw new Error("Не удалось загрузить диагностику.")
  return (await response.json()) as DiagnosticsStatus
}
