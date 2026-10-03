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
  awaiting_observations: AwaitingObservation[]
}

export async function fetchDiagnosticsStatus(signal?: AbortSignal): Promise<DiagnosticsStatus> {
  const response = await apiFetch("/api/diagnostics/status", { signal })
  if (!response.ok) throw new Error("Не удалось загрузить диагностику.")
  return (await response.json()) as DiagnosticsStatus
}
