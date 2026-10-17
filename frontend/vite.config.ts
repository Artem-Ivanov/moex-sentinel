import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"

const configDirectory = dirname(fileURLToPath(import.meta.url))
const versionPattern = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/
const versionError = "Frontend build blocked: package.json and package-lock.json versions must match MAJOR.MINOR.PATCH."
const defaultDevApiProxyTarget = "http://localhost:8000"

export function resolveDevApiProxyTarget(value: string | undefined): string {
  const target = value || defaultDevApiProxyTarget
  let url: URL
  try {
    url = new URL(target)
  } catch {
    throw new Error("DEV_API_PROXY_TARGET must be an HTTP(S) origin without credentials, path, query, or fragment.")
  }

  if (
    !["http:", "https:"].includes(url.protocol)
    || !url.hostname
    || url.username
    || url.password
    || url.pathname !== "/"
    || url.search
    || url.hash
  ) {
    throw new Error("DEV_API_PROXY_TARGET must be an HTTP(S) origin without credentials, path, query, or fragment.")
  }
  return url.origin
}

function checkFrontendVersionMetadata(): void {
  let packageVersion: unknown
  let lockVersion: unknown
  let lockRootVersion: unknown
  try {
    const packageJson = JSON.parse(readFileSync(resolve(configDirectory, "package.json"), "utf8"))
    const packageLock = JSON.parse(readFileSync(resolve(configDirectory, "package-lock.json"), "utf8"))
    packageVersion = packageJson.version
    lockVersion = packageLock.version
    lockRootVersion = packageLock.packages?.[""]?.version
  } catch {
    throw new Error(versionError)
  }

  if ([packageVersion, lockVersion, lockRootVersion].some((version) => typeof version !== "string" || version.length > 32 || !versionPattern.test(version))
    || packageVersion !== lockVersion || packageVersion !== lockRootVersion) {
    throw new Error(versionError)
  }
}

export default defineConfig(({ command }) => {
  if (command === "build") checkFrontendVersionMetadata()
  const apiProxyTarget = command === "serve"
    ? resolveDevApiProxyTarget(process.env.DEV_API_PROXY_TARGET)
    : defaultDevApiProxyTarget
  return {
    plugins: [vue()],
    server: {
      proxy: {
        "/api": apiProxyTarget,
      },
    },
  }
})
