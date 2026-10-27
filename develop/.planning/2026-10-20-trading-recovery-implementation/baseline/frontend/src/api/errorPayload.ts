export interface ApiFieldError {
  path: string
  code: string
  message: string
}

interface ApiErrorPayload {
  detail?: { code?: string; message?: string; fields?: ApiFieldError[] }
}

/** Read the backend envelope; each API client retains its own error policy. */
export async function readErrorPayload(response: Response): Promise<ApiErrorPayload> {
  return await response.json().catch(() => ({})) as ApiErrorPayload
}
