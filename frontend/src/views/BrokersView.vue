<script setup lang="ts">
import { onMounted, ref } from "vue"
import { useRouter } from "vue-router"
import {
  BrokerApiError,
  checkBroker,
  createBroker,
  deleteBroker,
  fetchBrokerSettings,
  replaceBroker,
  type Broker,
  type BrokerAdapterDefinition,
  type BrokerDraft,
  type BrokerSettings,
} from "../api/brokers"
import BrokerForm from "../components/BrokerForm.vue"

const router = useRouter()
const data = ref<BrokerSettings>()
const error = ref("")
const fieldErrors = ref<Record<string, string>>({})
const formOpen = ref(false)
const formMode = ref<"create" | "edit">("create")
const editingBrokerId = ref<string>()
const selectedBrokerId = ref<string>()
const selected = ref<BrokerAdapterDefinition>()
const status = ref<Record<string, string>>({})

function emptyDraft(): BrokerDraft {
  return {
    display_name: "",
    provider_code: "",
    environment_code: "",
    adapter_code: "",
    enabled: true,
    fields: [],
    is_test: true,
    account_id: null,
  }
}

const draft = ref<BrokerDraft>(emptyDraft())

async function load() {
  const settings = await fetchBrokerSettings()
  data.value = settings
  if (selectedBrokerId.value && !settings.brokers.some((broker) => broker.id === selectedBrokerId.value)) {
    selectedBrokerId.value = undefined
  }
}

onMounted(async () => {
  try { await load() } catch { error.value = "Не удалось загрузить брокеров." }
})

function clearField(path: string) { delete fieldErrors.value[path] }

function copyDraft(broker: Broker): BrokerDraft {
  return {
    display_name: broker.display_name,
    provider_code: broker.provider_code,
    environment_code: broker.environment_code,
    adapter_code: broker.adapter_code,
    enabled: broker.enabled,
    fields: broker.fields.map((field) => ({ ...field })),
    is_test: broker.is_test,
    account_id: broker.account_id,
  }
}

function openCreate(): void {
  error.value = ""
  fieldErrors.value = {}
  selected.value = undefined
  editingBrokerId.value = undefined
  formMode.value = "create"
  draft.value = emptyDraft()
  formOpen.value = true
}

function openEdit(broker: Broker): void {
  error.value = ""
  fieldErrors.value = {}
  selectedBrokerId.value = broker.id
  selected.value = data.value?.adapters.find((item) => item.adapter_code === broker.adapter_code)
  editingBrokerId.value = broker.id
  formMode.value = "edit"
  draft.value = copyDraft(broker)
  formOpen.value = true
}

function selectBroker(brokerId: string): void {
  selectedBrokerId.value = brokerId
}

function closeForm(): void {
  error.value = ""
  fieldErrors.value = {}
  selected.value = undefined
  editingBrokerId.value = undefined
  formMode.value = "create"
  draft.value = emptyDraft()
  formOpen.value = false
}

function selectAdapter(code: string) {
  clearField("adapter_code")
  selected.value = data.value?.adapters.find((item) => item.adapter_code === code)
  if (!selected.value) return
  draft.value.adapter_code = code
  draft.value.provider_code = selected.value.provider_code
  draft.value.environment_code = selected.value.environment_code
  draft.value.fields = selected.value.fields.map((field) => ({
    name: field.name,
    value: field.default_value ?? "",
  }))
}

async function save() {
  error.value = ""
  fieldErrors.value = {}
  try {
    if (formMode.value === "edit" && editingBrokerId.value) {
      await replaceBroker(editingBrokerId.value, draft.value)
    } else {
      await createBroker(draft.value)
    }
    await load()
    closeForm()
  } catch (cause) {
    if (cause instanceof BrokerApiError) {
      error.value = cause.message
      fieldErrors.value = Object.fromEntries(cause.fields.map((item) => [item.path, item.message]))
    } else {
      error.value = "Не удалось сохранить брокера."
    }
  }
}

async function check(id: string) {
  try {
    const result = await checkBroker(id)
    status.value[id] = `Доступно счетов: ${result.accounts_count}`
  } catch (cause) {
    status.value[id] = cause instanceof Error ? cause.message : "Проверка не выполнена."
  }
}

async function remove(id: string) {
  error.value = ""
  try {
    await deleteBroker(id)
    await load()
  } catch (cause) {
    error.value = cause instanceof BrokerApiError ? cause.message : "Не удалось удалить брокера."
  }
}
</script>

<template>
  <section class="page">
    <p class="eyebrow">Интеграции</p>
    <div class="page-title">
      <h2>Брокеры</h2>
      <button @click="openCreate">Добавить брокера</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="!data && !error">Загрузка…</p>
    <template v-if="data">
      <div v-if="data.brokers.length === 0" class="empty">Подключённые брокеры отсутствуют</div>
      <article
        v-for="broker in data.brokers"
        :key="broker.id"
        class="grid-row broker-row"
        :class="{ 'data-table__row--selected': selectedBrokerId === broker.id }"
        tabindex="0"
        @click="selectBroker(broker.id)"
        @dblclick="openEdit(broker)"
        @keydown.enter.prevent="selectBroker(broker.id)"
      >
        <div>
          <strong>{{ broker.display_name }}</strong>
          <small>{{ broker.provider_code }} · {{ broker.environment_code }}</small>
          <p v-if="status[broker.id]" class="hint">{{ status[broker.id] }}</p>
        </div>
        <div class="actions" @dblclick.stop>
          <button @click.stop="check(broker.id)">Проверить</button>
          <button @click.stop="router.push({ name: 'broker-accounts', params: { brokerId: broker.id } })">Счета</button>
          <button @click.stop="remove(broker.id)">Удалить</button>
        </div>
      </article>
    </template>
    <BrokerForm
      v-if="formOpen"
      v-model:draft="draft"
      :mode="formMode"
      :adapters="data?.adapters ?? []"
      :field-errors="fieldErrors"
      @select-adapter="selectAdapter"
      @clear-field="clearField"
      @submit="save"
      @cancel="closeForm"
    />
  </section>
</template>
