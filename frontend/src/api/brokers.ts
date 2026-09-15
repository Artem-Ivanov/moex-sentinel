import { readErrorPayload, type ApiFieldError } from "./errorPayload"

export interface BrokerField {
  name: string
  value: string
}

export interface BrokerDraft {
  display_name: string
  provider_code: string
  environment_code: string
  adapter_code: string
  enabled: boolean
  fields: BrokerField[]
  is_test: boolean
  account_id: string | null
}

export interface Broker extends BrokerDraft {
  id: string
  created_at: string
  updated_at: string
}

export interface BrokerFieldDefinition {
  name: string
  required: boolean
  default_value: string | null
}

export interface BrokerAdapterDefinition {
  adapter_code: string
  provider_code: string
  environment_code: string
  fields: BrokerFieldDefinition[]
}

export interface BrokerSettings {
  adapters: BrokerAdapterDefinition[]
  brokers: Broker[]
}

export interface BrokerConnectionStatus {
  broker_id: string
  available: boolean
  accounts_count: number
}

export class BrokerApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly fields: ApiFieldError[] = [],
  ) {
    super(message)
  }
}

async function ensureSuccess(response: Response): Promise<Response> {
  if (response.ok) return response
  const payload = await readErrorPayload(response)
  const detail = payload?.detail
  if (detail?.code && detail.message) {
    throw new BrokerApiError(detail.code, detail.message, detail.fields ?? [])
  }
  throw new BrokerApiError("BROKER_API_ERROR", "Не удалось выполнить запрос к backend.")
}

export async function fetchBrokerSettings(): Promise<BrokerSettings> {
  const response = await ensureSuccess(await fetch("/api/brokers"))
  return (await response.json()) as BrokerSettings
}

export async function createBroker(draft: BrokerDraft): Promise<Broker> {
  const response = await ensureSuccess(
    await fetch("/api/brokers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    }),
  )
  return (await response.json()) as Broker
}

export async function replaceBroker(brokerId: string, draft: BrokerDraft): Promise<Broker> {
  const response = await ensureSuccess(
    await fetch(`/api/brokers/${brokerId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    }),
  )
  return (await response.json()) as Broker
}

export async function deleteBroker(brokerId: string): Promise<void> {
  await ensureSuccess(await fetch(`/api/brokers/${brokerId}`, { method: "DELETE" }))
}

export async function checkBroker(brokerId: string): Promise<BrokerConnectionStatus> {
  const response = await ensureSuccess(
    await fetch(`/api/brokers/${brokerId}/check`, { method: "POST" }),
  )
  return (await response.json()) as BrokerConnectionStatus
}
