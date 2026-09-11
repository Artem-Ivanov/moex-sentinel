import { afterEach, describe, expect, it, vi } from "vitest"

import { fetchHealth } from "./health"

describe("fetchHealth", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("returns the backend response without duplicating backend validation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "ok",
            service: "backend",
            version: "0.1.0",
            database: "ok",
            schema: "compatible",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    )

    await expect(fetchHealth()).resolves.toEqual({
      status: "ok",
      service: "backend",
      version: "0.1.0",
      database: "ok",
      schema: "compatible",
    })
  })

  it("accepts the database health field", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "ok",
            service: "backend",
            version: "0.1.0",
            database: "ok",
            schema: "compatible",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    )

    await expect(fetchHealth()).resolves.toMatchObject({ database: "ok", schema: "compatible" })
  })
})
