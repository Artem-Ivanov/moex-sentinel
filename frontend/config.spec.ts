import { describe, expect, it, vi } from "vitest"
import viteConfig, { resolveDevApiProxyTarget } from "./vite.config"

describe("development API proxy configuration", () => {
  it("uses the local Core origin by default and accepts an explicit origin", () => {
    expect(resolveDevApiProxyTarget(undefined)).toBe("http://localhost:8000")
    expect(resolveDevApiProxyTarget("https://core.example.test")).toBe("https://core.example.test")
  })

  it.each([
    "ftp://core.example.test",
    "https://user:secret@core.example.test",
    "https://core.example.test/api",
    "https://core.example.test?token=secret",
    "https://core.example.test#fragment",
    "not a URL",
  ])("rejects non-origin target %s", (target) => {
    expect(() => resolveDevApiProxyTarget(target)).toThrow(
      "DEV_API_PROXY_TARGET must be an HTTP(S) origin without credentials, path, query, or fragment.",
    )
  })

  it("keeps the target in the Vite server proxy configuration", () => {
    const config = typeof viteConfig === "function"
      ? viteConfig({ command: "serve", mode: "development" })
      : viteConfig

    expect(config.server?.proxy?.["/api"]).toBe("http://localhost:8000")
  })

  it("does not read the development target during a production build", () => {
    vi.stubEnv("DEV_API_PROXY_TARGET", "ftp://invalid.example")
    const buildConfig = () => typeof viteConfig === "function"
      ? viteConfig({ command: "build", mode: "production" })
      : viteConfig

    try {
      expect(buildConfig).not.toThrow()
    } finally {
      vi.unstubAllEnvs()
    }
  })
})
