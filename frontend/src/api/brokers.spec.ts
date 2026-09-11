import { afterEach, describe, expect, it, vi } from "vitest"

import {
  createBroker,
  checkBroker,
  deleteBroker,
  fetchBrokerSettings,
  replaceBroker,
  type BrokerDraft,
} from "./brokers"

const draft: BrokerDraft = {
  display_name: "Primary sandbox",
  provider_code: "TINVEST",
  environment_code: "SANDBOX",
  adapter_code: "TINVEST_SANDBOX",
  enabled: true,
  fields: [
    { name: "token", value: "synthetic-token" },
    { name: "fqdn", value: "sandbox-invest-public-api.tbank.ru:443" },
  ],
  is_test: true,
  account_id: "account-1",
}

describe("broker API client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("fetches broker settings without response validation", async () => {
    const payload = { adapters: [], brokers: [] }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal("fetch", fetchMock)

    await expect(fetchBrokerSettings()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith("/api/brokers")
  })

  it("creates and replaces a broker with unchanged input", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(Response.json({ id: "broker-1" })),
    )
    vi.stubGlobal("fetch", fetchMock)

    await createBroker(draft)
    await replaceBroker("broker-1", draft)

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/brokers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    })
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/brokers/broker-1", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    })
  })

  it("deletes a broker through its resource URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal("fetch", fetchMock)

    await deleteBroker("broker-1")

    expect(fetchMock).toHaveBeenCalledWith("/api/brokers/broker-1", { method: "DELETE" })
  })

  it("checks a broker through its action URL", async () => {
    const payload = { broker_id: "broker-1", available: true, accounts_count: 1 }
    const fetchMock = vi.fn().mockResolvedValue(Response.json(payload))
    vi.stubGlobal("fetch", fetchMock)

    await expect(checkBroker("broker-1")).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith("/api/brokers/broker-1/check", { method: "POST" })
  })

  it("surfaces a safe backend transport error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          { detail: { code: "INVALID_BROKER_FIELDS", message: "Проверьте настройки площадки." } },
          { status: 422 },
        ),
      ),
    )

    await expect(createBroker(draft)).rejects.toMatchObject({
      code: "INVALID_BROKER_FIELDS",
      message: "Проверьте настройки площадки.",
    })
  })
})
