export interface HealthResponse {
  status: "ok"
  service: "backend"
  version: string
  database: "ok"
  schema: "compatible"
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch("/api/health", { signal })
  if (!response.ok) {
    throw new Error("Backend health check failed")
  }
  return (await response.json()) as HealthResponse
}
