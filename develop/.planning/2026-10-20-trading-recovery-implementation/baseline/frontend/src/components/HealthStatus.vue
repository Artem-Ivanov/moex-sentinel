<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue"

import { fetchHealth } from "../api/health"

type HealthState =
  | { status: "loading" }
  | { status: "online"; version: string }
  | { status: "offline" }

const health = ref<HealthState>({ status: "loading" })
const controller = new AbortController()

onMounted(async () => {
  try {
    const response = await fetchHealth(controller.signal)
    health.value = { status: "online", version: response.version }
  } catch (error: unknown) {
    if (!(error instanceof DOMException && error.name === "AbortError")) {
      health.value = { status: "offline" }
    }
  }
})

onUnmounted(() => {
  controller.abort()
})
</script>

<template>
  <div class="health-status" :data-status="health.status" role="status" aria-live="polite">
    <span class="health-status__dot" aria-hidden="true" />
    <span v-if="health.status === 'loading'">Проверка backend…</span>
    <template v-else-if="health.status === 'online'">
      <span>Backend доступен</span>
      <span class="health-status__version">v{{ health.version }}</span>
    </template>
    <span v-else>Backend недоступен</span>
  </div>
</template>
