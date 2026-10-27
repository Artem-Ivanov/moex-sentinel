<script setup lang="ts">
import { ref } from "vue"
import { useRoute, useRouter } from "vue-router"

import { AuthError, login, safeInternalRedirect } from "../auth"

const route = useRoute()
const router = useRouter()
const username = ref("")
const password = ref("")
const error = ref("")
const submitting = ref(false)

async function submit(): Promise<void> {
  error.value = ""
  submitting.value = true
  try {
    await login(username.value, password.value)
  } catch (reason) {
    if (!(reason instanceof AuthError)) {
      error.value = "Не удалось связаться с сервером. Проверьте подключение."
    } else if (reason.status === 401) {
      error.value = "Не удалось войти. Проверьте имя пользователя и пароль."
    } else if (reason.status === 429) {
      error.value = "Слишком много попыток. Попробуйте позже."
    } else {
      error.value = "Сервер не смог выполнить вход. Попробуйте позже."
    }
    return
  } finally {
    password.value = ""
    submitting.value = false
  }
  await router.replace(safeInternalRedirect(route.query.redirect) ?? { name: "positions" })
}
</script>

<template>
  <main class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <p class="eyebrow">MOEX Sentinel</p>
      <h1>Вход в систему</h1>
      <label>
        Имя пользователя
        <input v-model="username" name="username" autocomplete="username" required autofocus>
      </label>
      <label>
        Пароль
        <input v-model="password" name="password" type="password" autocomplete="current-password" required>
      </label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button type="submit" :disabled="submitting">{{ submitting ? "Входим…" : "Войти" }}</button>
    </form>
  </main>
</template>
