<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { useRouter } from "vue-router"

import { fetchBrokerSettings, type Broker } from "../api/brokers"
import {
  fetchInstruments,
  setInstrumentSelection,
  synchronizeInstruments,
  type CatalogInstrument,
  type CatalogListResponse,
} from "../api/instruments"

const router = useRouter()
const brokers = ref<Broker[]>([])
const brokerId = ref("")
const data = ref<CatalogListResponse>()
const category = ref<string | null>(null)
const selectedOnly = ref(false)
const searchQuery = ref("")
const lotPriceFrom = ref("")
const lotPriceTo = ref("")
const currency = ref("RUB")
const selectedRow = ref<string | null>(null)
const loading = ref(false)
const error = ref("")
const syncMessage = ref("")
const visibleItems = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase()
  if (!query) return data.value?.items ?? []
  return (data.value?.items ?? []).filter((item) =>
    item.ticker.toLocaleLowerCase().includes(query)
      || item.name.toLocaleLowerCase().includes(query),
  )
})

onMounted(async () => {
  try {
    const settings = await fetchBrokerSettings()
    brokers.value = settings.brokers.filter((broker) => broker.enabled)
    brokerId.value = brokers.value[0]?.id ?? ""
    if (brokerId.value) await loadCatalog()
  } catch {
    error.value = "Не удалось загрузить справочник инструментов."
  }
})

async function loadCatalog(): Promise<void> {
  if (!brokerId.value) return
  loading.value = true
  error.value = ""
  try {
    data.value = await fetchInstruments(
      brokerId.value,
      category.value,
      false,
      selectedOnly.value,
      lotPriceFrom.value,
      lotPriceTo.value,
      currency.value,
    )
    selectedRow.value = null
  } catch (caught: unknown) {
    error.value = caught instanceof Error ? caught.message : "Не удалось загрузить инструменты."
  } finally {
    loading.value = false
  }
}

async function chooseCategory(value: string | null): Promise<void> {
  category.value = value
  await loadCatalog()
}

async function toggleSelectedFilter(): Promise<void> {
  selectedOnly.value = !selectedOnly.value
  await loadCatalog()
}

async function resetFilters(): Promise<void> {
  searchQuery.value = ""
  category.value = null
  selectedOnly.value = false
  lotPriceFrom.value = ""
  lotPriceTo.value = ""
  currency.value = "RUB"
  await loadCatalog()
}

async function applyLotPriceFilter(): Promise<void> {
  await loadCatalog()
}

async function synchronize(): Promise<void> {
  loading.value = true
  error.value = ""
  syncMessage.value = ""
  try {
    const result = await synchronizeInstruments(brokerId.value)
    syncMessage.value = `Добавлено: ${result.added}, обновлено: ${result.updated}, деактивировано: ${result.deactivated}`
    await loadCatalog()
  } catch (caught: unknown) {
    error.value = caught instanceof Error ? caught.message : "Не удалось обновить справочник."
    loading.value = false
  }
}

async function toggleSelection(item: CatalogInstrument): Promise<void> {
  error.value = ""
  try {
    const updated = await setInstrumentSelection(
      item.broker_id,
      item.id,
      !item.is_selected,
    )
    Object.assign(item, updated)
  } catch {
    error.value = "Не удалось изменить отбор инструмента."
  }
}

function openDetails(item?: CatalogInstrument): void {
  const target = item ?? data.value?.items.find((value) => value.id === selectedRow.value)
  if (target) {
    void router.push({
      name: "instrument-details",
      params: { brokerId: target.broker_id, instrumentId: target.id },
    })
  }
}
</script>

<template>
  <section class="page">
    <div class="page-title">
      <div><p class="eyebrow">Справочник площадки</p><h2>Инструменты</h2></div>
      <button :disabled="loading || !brokerId" @click="synchronize">
        {{ data?.sync_state.status === "NOT_STARTED" ? "Загрузить инструменты" : "Обновить" }}
      </button>
    </div>
    <div class="toolbar instrument-toolbar">
      <label>Брокер
        <select v-model="brokerId" :disabled="loading" @change="loadCatalog">
          <option v-for="broker in brokers" :key="broker.id" :value="broker.id">{{ broker.display_name }}</option>
        </select>
      </label>
      <label>Поиск
        <input
          v-model="searchQuery"
          class="instrument-search"
          type="search"
          aria-label="Поиск по тикеру или названию"
          placeholder="Тикер или название"
        >
      </label>
      <label>Валюта
        <select v-model="currency" :disabled="loading" aria-label="Валюта инструментов" @change="loadCatalog">
          <option v-for="value in (data?.currencies ?? ['RUB'])" :key="value" :value="value">{{ value }}</option>
        </select>
      </label>
      <label>Цена лота от
        <input v-model="lotPriceFrom" class="lot-price-filter" type="text" inputmode="decimal">
      </label>
      <label>Цена лота до
        <input v-model="lotPriceTo" class="lot-price-filter" type="text" inputmode="decimal">
      </label>
      <button :disabled="loading" @click="applyLotPriceFilter">Применить цену</button>
      <button :class="{ 'filter-button--active': selectedOnly }" @click="toggleSelectedFilter">Только выделенные</button>
      <button @click="resetFilters">Сбросить фильтры</button>
      <button :disabled="!selectedRow" @click="openDetails()">Подробнее</button>
    </div>
    <div v-if="data" class="catalog-tabs" aria-label="Типы инструментов">
      <button :class="{ 'filter-button--active': category === null }" @click="chooseCategory(null)">Все</button>
      <button
        v-for="item in data.categories"
        :key="item.code"
        :class="{ 'filter-button--active': category === item.code }"
        @click="chooseCategory(item.code)"
      >{{ item.label }} {{ item.count }}</button>
    </div>
    <p v-if="syncMessage" class="hint">{{ syncMessage }}</p>
    <p v-if="data?.sync_state.last_success_at" class="hint">Последняя синхронизация: {{ data.sync_state.last_success_at }}</p>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-else-if="loading && !data">Загрузка…</p>
    <div v-else-if="data && data.items.length === 0" class="empty">Справочник инструментов пуст</div>
    <div v-else-if="data && visibleItems.length === 0" class="empty">По вашему запросу инструменты не найдены</div>
    <div v-else-if="data" class="table-scroll">
      <table class="data-table">
        <thead><tr><th>Тикер</th><th>Название</th><th>Тип</th><th>Валюта</th><th>Лот</th><th>Цена за единицу</th><th>Цена за лот</th><th>Листинг</th><th>Отбор</th></tr></thead>
        <tbody>
          <tr
            v-for="item in visibleItems"
            :key="item.id"
            tabindex="0"
            :class="{
              'data-table__row--selected': selectedRow === item.id,
              'data-table__row--marked': item.is_selected,
            }"
            @click="selectedRow = item.id"
            @dblclick="openDetails(item)"
          >
            <td><strong>{{ item.ticker }}</strong></td><td>{{ item.name }}</td>
            <td>{{ data.categories.find((value) => value.code === item.category)?.label ?? item.category }}</td>
            <td>{{ item.currency ?? "—" }}</td><td>{{ item.lot }}</td>
            <td>{{ item.unit_price ?? "—" }}</td><td>{{ item.lot_price ?? "—" }}</td>
            <td>{{ item.is_active ? "Активен" : "Снят" }}</td>
            <td>
              <button
                class="selection-switch"
                :class="{ 'selection-switch--active': item.is_selected }"
                role="switch"
                :aria-checked="item.is_selected"
                :aria-label="item.is_selected
                  ? `Исключить ${item.ticker} из отбора`
                  : `Добавить ${item.ticker} в отбор`"
                :title="item.is_selected ? 'Исключить из отбора' : 'Добавить в отбор'"
                @click.stop="toggleSelection(item)"
              ><span aria-hidden="true"></span></button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
