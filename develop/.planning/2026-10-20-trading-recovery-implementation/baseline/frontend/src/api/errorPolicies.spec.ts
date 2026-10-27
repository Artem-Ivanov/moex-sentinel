import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, closeAutomation, fetchAutomations } from "./automations"
import { BrokerApiError, fetchBrokerSettings } from "./brokers"
import { MarketDataApiError, searchMarketInstruments } from "./marketData"

const fields = [{ path: "account_id", code: "REQUIRED", message: "Укажите счёт." }]
const consumers = [
  {
    name: "brokers",
    request: fetchBrokerSettings,
    errorType: BrokerApiError,
    fallbackCode: "BROKER_API_ERROR",
    fallbackMessage: "Не удалось выполнить запрос к backend.",
  },
  {
    name: "market data",
    request: () => searchMarketInstruments("broker-1", "SBER", 20),
    errorType: MarketDataApiError,
    fallbackCode: "MARKET_DATA_API_ERROR",
    fallbackMessage: "Не удалось получить рыночные данные.",
  },
]

afterEach(() => vi.unstubAllGlobals())

describe.each([
  ...consumers.map((consumer) => ({ ...consumer, exposesCode: true })),
  { name: "automations", request: fetchAutomations, errorType: ApiError, exposesCode: false },
])("$name public error contract", ({ request, errorType, exposesCode }) => {
  it("preserves the typed backend error and field details", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({
      detail: { code: "INVALID_FIELDS", message: "Проверьте поля.", fields },
    }, { status: 422 })))

    const error = await request().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(errorType)
    expect(error).toMatchObject({ message: "Проверьте поля.", fields })
    if (exposesCode) expect(error).toHaveProperty("code", "INVALID_FIELDS")
    else expect(error).not.toHaveProperty("code")
  })

  it("propagates the original network rejection", async () => {
    const failure = new TypeError("Network unavailable")
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(failure))

    await expect(request()).rejects.toBe(failure)
  })
})

describe.each(consumers)("$name requires both code and nonempty message", (consumer) => {
  it.each([
    { name: "message without code", body: JSON.stringify({ detail: { message: "Explanation", fields } }) },
    { name: "empty message", body: JSON.stringify({ detail: { code: "INVALID_FIELDS", message: "", fields } }) },
    { name: "non-JSON body", body: "upstream unavailable" },
    { name: "JSON null", body: "null" },
  ])("uses the fallback for $name", async ({ body }) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 422 })))

    const error = await consumer.request().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(consumer.errorType)
    expect(error).toMatchObject({
      code: consumer.fallbackCode,
      message: consumer.fallbackMessage,
      fields: [],
    })
  })
})

describe("automation error policy", () => {
  it.each([
    { name: "message without code", body: JSON.stringify({ detail: { message: "Explanation", fields } }), message: "Explanation", expectedFields: fields },
    { name: "empty message", body: JSON.stringify({ detail: { code: "INVALID_FIELDS", message: "", fields } }), message: "", expectedFields: fields },
    { name: "non-JSON body", body: "upstream unavailable", message: "Не удалось выполнить запрос.", expectedFields: [] },
  ])("preserves its policy for $name", async ({ body, message, expectedFields }) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 422 })))

    const error = await fetchAutomations().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ message, fields: expectedFields })
  })

  it("retains the existing TypeError for a JSON null error body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(null, { status: 422 })))

    await expect(fetchAutomations()).rejects.toBeInstanceOf(TypeError)
  })

  it("accepts a 204 lifecycle response without parsing its empty body", async () => {
    const response = new Response(null, { status: 204 })
    const parseBody = vi.spyOn(response, "json")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response))

    await expect(closeAutomation("automation-1")).resolves.toBeUndefined()
    expect(parseBody).not.toHaveBeenCalled()
  })
})
