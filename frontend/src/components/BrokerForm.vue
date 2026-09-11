<script setup lang="ts">
import type { BrokerAdapterDefinition, BrokerDraft } from "../api/brokers"

export type BrokerFormMode = "create" | "edit"

const props = defineProps<{
  mode: BrokerFormMode
  draft: BrokerDraft
  adapters: BrokerAdapterDefinition[]
  fieldErrors: Record<string, string>
}>()

const emit = defineEmits<{
  "update:draft": [value: BrokerDraft]
  "select-adapter": [adapterCode: string]
  "clear-field": [path: string]
  submit: []
  cancel: []
}>()

function valueFrom(event: Event): string {
  return (event.target as HTMLInputElement | HTMLSelectElement).value
}

function checkedFrom(event: Event): boolean {
  return (event.target as HTMLInputElement).checked
}

function updateScalar<Key extends keyof BrokerDraft>(key: Key, value: BrokerDraft[Key]): void {
  emit("update:draft", { ...props.draft, [key]: value })
  emit("clear-field", String(key))
}

function updateConnectionField(index: number, value: string): void {
  const current = props.draft.fields[index]
  const fields = props.draft.fields.map((field, itemIndex) =>
    itemIndex === index ? { ...field, value } : field,
  )
  emit("update:draft", { ...props.draft, fields })
  emit("clear-field", `fields.${current.name}`)
}

function selectAdapter(event: Event): void {
  const adapterCode = valueFrom(event)
  emit("clear-field", "adapter_code")
  emit("select-adapter", adapterCode)
}
</script>

<template>
  <form class="form-card" @submit.prevent="emit('submit')">
    <h3>{{ mode === "edit" ? "Редактирование интеграции" : "Новая интеграция" }}</h3>
    <label>Название
      <input
        :value="draft.display_name"
        :aria-invalid="!!fieldErrors.display_name"
        @input="updateScalar('display_name', valueFrom($event))"
      >
      <small v-if="fieldErrors.display_name" class="field-error">{{ fieldErrors.display_name }}</small>
    </label>
    <label>Адаптер
      <select
        :value="draft.adapter_code"
        :disabled="mode === 'edit'"
        :aria-invalid="!!fieldErrors.adapter_code"
        @change="selectAdapter"
      >
        <option value="">Выберите адаптер</option>
        <option
          v-for="adapter in adapters"
          :key="adapter.adapter_code"
          :value="adapter.adapter_code"
        >{{ adapter.provider_code }} · {{ adapter.environment_code }}</option>
      </select>
      <small v-if="fieldErrors.adapter_code" class="field-error">{{ fieldErrors.adapter_code }}</small>
    </label>
    <label class="checkbox">
      <input
        :checked="draft.enabled"
        type="checkbox"
        @change="updateScalar('enabled', checkedFrom($event))"
      > Включено
    </label>
    <small v-if="fieldErrors.enabled" class="field-error">{{ fieldErrors.enabled }}</small>
    <label class="checkbox">
      <input
        :checked="draft.is_test"
        :disabled="mode === 'edit'"
        type="checkbox"
        @change="updateScalar('is_test', checkedFrom($event))"
      > Тестовое подключение
    </label>
    <small v-if="fieldErrors.is_test" class="field-error">{{ fieldErrors.is_test }}</small>
    <label>Идентификатор брокерского счёта
      <input
        :value="draft.account_id ?? ''"
        :aria-invalid="!!fieldErrors.account_id"
        @input="updateScalar('account_id', valueFrom($event) || null)"
      >
      <small v-if="fieldErrors.account_id" class="field-error">{{ fieldErrors.account_id }}</small>
    </label>
    <label v-for="(field, index) in draft.fields" :key="field.name">{{ field.name }}
      <input
        :value="field.value"
        :aria-label="field.name"
        :aria-invalid="!!fieldErrors[`fields.${field.name}`]"
        @input="updateConnectionField(index, valueFrom($event))"
      >
      <small v-if="fieldErrors[`fields.${field.name}`]" class="field-error">
        {{ fieldErrors[`fields.${field.name}`] }}
      </small>
    </label>
    <div class="actions">
      <button type="submit">Сохранить</button>
      <button type="button" @click="emit('cancel')">Отмена</button>
    </div>
  </form>
</template>
