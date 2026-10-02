import { afterEach, expect, it, vi } from "vitest"
import { loadRuntime, runtime, tradingAllowed } from "./runtime"

afterEach(() => { runtime.value = null; vi.unstubAllGlobals() })
it("allows commands only after authenticated runtime explicitly permits TRADE", async () => {
  expect(tradingAllowed.value).toBe(false)
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ environment: "PROD", access_mode: "READ_ONLY" }))))
  await loadRuntime()
  expect(runtime.value?.environment).toBe("PROD")
  expect(tradingAllowed.value).toBe(false)
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ environment: "TEST", access_mode: "TRADE" }))))
  await loadRuntime()
  expect(tradingAllowed.value).toBe(true)
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 })))
  await loadRuntime()
  expect(tradingAllowed.value).toBe(false)
})
