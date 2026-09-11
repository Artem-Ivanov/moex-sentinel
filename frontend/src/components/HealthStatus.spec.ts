import { render, screen } from "@testing-library/vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import * as healthApi from "../api/health"
import HealthStatus from "./HealthStatus.vue"

describe("HealthStatus", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("shows backend online after a successful health request", async () => {
    vi.spyOn(healthApi, "fetchHealth").mockResolvedValue({
      status: "ok",
      service: "backend",
      version: "0.1.0",
      database: "ok",
      schema: "compatible",
    })

    render(HealthStatus)

    expect(await screen.findByText("Backend доступен")).toBeTruthy()
    expect(screen.getByText("v0.1.0")).toBeTruthy()
  })

  it("shows a safe offline state when health fails", async () => {
    vi.spyOn(healthApi, "fetchHealth").mockRejectedValue(new Error("network"))

    render(HealthStatus)

    expect(await screen.findByText("Backend недоступен")).toBeTruthy()
  })
})
