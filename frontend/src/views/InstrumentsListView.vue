<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue"
import { useRouter } from "vue-router"

import { fetchBrokerSettings, type Broker } from "../api/brokers"
import {
  fetchInstruments,
  setInstrumentSelection,
  synchronizeInstruments,
  type CatalogInstrument,
  type CatalogListResponse,
  type PositionAdoptionResult,
  type PositionAdoptionReason,
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
const readLoading = ref(false)
const syncing = ref(false)
const loading = computed(() => readLoading.value || syncing.value)
let active = true
let readGeneration = 0
let brokerScope = 0
onUnmounted(() => { active = false; readGeneration += 1; brokerScope += 1 })
const settingsLoaded = ref(false)
const error = ref("")
const syncMessage = ref("")
const adoption = ref<PositionAdoptionResult | null>(null)
watch(brokerId, () => {
  brokerScope += 1
  readGeneration += 1
  data.value = undefined
  selectedRow.value = null
  adoption.value = null
  syncMessage.value = ""
  error.value = ""
  readLoading.value = false
  syncing.value = false
}, { flush: "sync" })
const adoptionReasons: Record<PositionAdoptionReason, string> = {
  BOOTSTRAP_BLOCKED_INVENTORY: "Заблокированный остаток: нужна сверка доступного количества.",
  BOOTSTRAP_INVALID_QUANTITY: "Короткая позиция или некорректное количество (включая дробные лоты): автомат не создан.",
  BOOTSTRAP_PRICE_UNAVAILABLE: "Средняя цена недоступна: нужна сверка стоимости позиции.",
  BOOTSTRAP_INSTRUMENT_NOT_FOUND: "Инструмент не найден в каталоге: автомат не создан.",
  BOOTSTRAP_ACTIVE_ORDER: "Есть активная заявка: нужна сверка её состояния.",
  BOOTSTRAP_COMMISSION_UNAVAILABLE: "Комиссия недоступна: нужна сверка финансовых данных.",
  BOOTSTRAP_CURRENCY_MISMATCH: "Валюты позиции и инструмента не совпадают: нужна сверка.",
  BOOTSTRAP_CONFLICT: "Новый снимок не принят: сверьте уже учтённую позицию. Это не подтверждает остановку автомата.",
}
function adoptionReason(reason: PositionAdoptionReason): string {
  return adoptionReasons[reason] ?? "Неизвестная причина: проверьте результат сверки позиций."
}
function maskedAccount(account: string): string {
  return account.length > 4 ? `••••${account.slice(-4)}` : "••••"
}
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
    if (!active) return
    brokers.value = settings.brokers.filter((broker) => broker.enabled)
    brokerId.value = brokers.value[0]?.id ?? ""
    settingsLoaded.value = true
    if (brokerId.value) await loadCatalog()
  } catch {
    if (!active) return
    error.value = "Не удалось загрузить справочник инструментов."
  }
})

async function loadCatalog(): Promise<void> {
  if (!active || !brokerId.value) return
  const generation = ++readGeneration
  const scope = brokerScope
  const request = {
    brokerId: brokerId.value,
    category: category.value,
    selectedOnly: selectedOnly.value,
    lotPriceFrom: lotPriceFrom.value,
    lotPriceTo: lotPriceTo.value,
    currency: currency.value,
  }
  const current = () => active && generation === readGeneration && scope === brokerScope
  readLoading.value = true
  error.value = ""
  try {
    const result = await fetchInstruments(
      request.brokerId, request.category, false, request.selectedOnly,
      request.lotPriceFrom, request.lotPriceTo, request.currency,
    )
    if (!current()) return
    data.value = result
    if (!result.items.some((item) => item.id === selectedRow.value)) selectedRow.value = null
  } catch (caught: unknown) {
    if (current()) error.value = caught instanceof Error ? caught.message : "Не удалось загрузить инструменты."
  } finally {
    if (current()) readLoading.value = false
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

async function synchronize(): Promise<void> {
  if (!active || !brokerId.value || syncing.value) return
  const scope = brokerScope
  const requestBrokerId = brokerId.value
  const current = () => active && scope === brokerScope
  syncing.value = true
  error.value = ""
  syncMessage.value = ""
  adoption.value = null
  try {
    const result = await synchronizeInstruments(requestBrokerId)
    if (!current()) return
    adoption.value = result.adoption ?? null
    syncMessage.value = `Добавлено: ${result.added}, обновлено: ${result.updated}, деактивировано: ${result.deactivated}`
    void loadCatalog()
  } catch (caught: unknown) {
    if (current()) error.value = caught instanceof Error ? caught.message : "Не удалось обновить справочник."
  } finally {
    if (current()) syncing.value = false
  }
}

async function toggleSelection(item: CatalogInstrument): Promise<void> {
  const scope = brokerScope
  const current = () => active && scope === brokerScope && data.value?.items.some((value) => value === item)
  if (!current()) return
  const request = { brokerId: item.broker_id, id: item.id, selected: !item.is_selected }
  error.value = ""
  try {
    const updated = await setInstrumentSelection(request.brokerId, request.id, request.selected)
    if (current()) Object.assign(item, updated)
  } catch {
    if (current()) error.value = "Не удалось изменить отбор инструмента."
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
        <select v-model="brokerId" @change="loadCatalog">
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
        <select v-model="currency" aria-label="Валюта инструментов" @change="loadCatalog">
          <option v-for="value in (data?.currencies ?? ['RUB'])" :key="value" :value="value">{{ value }}</option>
        </select>
      </label>
      <label>Цена лота от
        <input v-model="lotPriceFrom" class="lot-price-filter" type="text" inputmode="decimal">
      </label>
      <label>Цена лота до
        <input v-model="lotPriceTo" class="lot-price-filter" type="text" inputmode="decimal">
      </label>
      <button @click="loadCatalog">Применить цену</button>
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
    <section v-if="adoption" aria-label="Результат сверки позиций">
      <p>Позиции: принято {{ adoption.adopted }}, уже учтено {{ adoption.existing }}, требуют сверки {{ adoption.held }}, пропущено {{ adoption.skipped }}.</p>
      <div v-if="adoption.held || adoption.skipped || adoption.diagnostics.length" class="error" role="alert">
        <p>Некоторые позиции требуют сверки. Успешное обновление каталога не означает принятия всех позиций.</p>
        <ul v-if="adoption.diagnostics.length">
          <li v-for="(diagnostic, index) in adoption.diagnostics" :key="index">
            Счёт {{ maskedAccount(diagnostic.account_id) }} · инструмент {{ diagnostic.external_instrument_id }}:
            {{ adoptionReason(diagnostic.reason) }}
          </li>
        </ul>
      </div>
    </section>
    <p v-if="data?.sync_state.last_success_at" class="hint">Последняя синхронизация: {{ data.sync_state.last_success_at }}</p>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-else-if="loading && !data">Загрузка…</p>
    <div v-else-if="settingsLoaded && brokers.length === 0" class="empty">
      <p>Чтобы загрузить инструменты, настройте подключение брокера и выберите счёт.</p>
      <RouterLink :to="{ name: 'brokers' }">Настроить брокера</RouterLink>
    </div>
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
