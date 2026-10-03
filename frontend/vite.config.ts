import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"

const configDirectory = dirname(fileURLToPath(import.meta.url))
const versionPattern = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/
const versionError = "Frontend build blocked: package.json and package-lock.json versions must match MAJOR.MINOR.PATCH."

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
  return {
    plugins: [vue()],
    server: {
      proxy: {
        "/api": "http://localhost:8000",
      },
    },
  }
})
