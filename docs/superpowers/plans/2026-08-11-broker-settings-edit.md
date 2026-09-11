# Broker Settings Edit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user edit mutable settings of an existing broker through the same form used to create a broker.

**Architecture:** Extract the current broker form into a controlled thin Vue component. `BrokersView` continues to own API orchestration and chooses the existing `createBroker` or `replaceBroker` client call from explicit create/edit state; Core remains the sole validation owner.

**Tech Stack:** Vue 3 Composition API, TypeScript, Vue Router, Testing Library Vue, Vitest, existing FastAPI broker contract.

## Global Constraints

- Reuse one form for create and edit; do not add a broker edit route.
- Adapter code, provider code, environment code and test/production contour are immutable in edit mode.
- Display name, enabled state, initial balance and adapter-defined connection fields are editable.
- Frontend performs no business validation or normalization.
- Backend field errors remain visible next to their exact form fields.
- Do not change `PUT /api/brokers/{broker_id}`, broker persistence, migrations or trading behavior.
- Do not mask or encrypt connection fields in this MVP.
- Frontend remains a thin client; the view may orchestrate requests but does not own business rules.
- Broker editing is entered only by double-clicking a row; a single click selects and highlights it without opening the form.
- The selected broker row is highlighted and a removed or missing selection is cleared.
- No git commits are created; each task ends with a review checkpoint without commit.
- Every accepted task runs its focused tests; final acceptance runs complete frontend and Python regression.

---

## File map

- Create `frontend/src/components/BrokerForm.vue`: render and emit create/edit form interactions only.
- Create `frontend/src/components/BrokerForm.spec.ts`: component contract for modes, immutable controls and controlled draft updates.
- Modify `frontend/src/views/BrokersView.vue`: own form mode, copied draft, selected broker ID, API calls and backend errors.
- Modify `frontend/src/views/BrokersView.spec.ts`: acceptance of edit, cancel, backend errors and preserved create flow.
- Modify `docs/superpowers/plans/2026-08-10-decoupled-database-runtime.md`: insert this feature as an explicit intermediate checkpoint before live cutover resumes.

---

### Task 1: Reusable controlled broker form

**Files:**

- Create: `frontend/src/components/BrokerForm.vue`
- Create: `frontend/src/components/BrokerForm.spec.ts`
- Read: `frontend/src/api/brokers.ts`

**Interfaces:**

- Consumes:

```ts
type BrokerFormMode = "create" | "edit"

interface BrokerFormProps {
  mode: BrokerFormMode
  draft: BrokerDraft
  adapters: BrokerAdapterDefinition[]
  fieldErrors: Record<string, string>
}
```

- Produces Vue events:

```ts
"update:draft": [value: BrokerDraft]
"select-adapter": [adapterCode: string]
"clear-field": [path: string]
"submit": []
"cancel": []
```

- The component never imports `createBroker`, `replaceBroker` or `fetch`.

- [x] **Step 1: Write failing component tests for edit mode**

Create `BrokerForm.spec.ts` with a complete draft containing `token` and `fqdn` connection fields. Render `BrokerForm` in edit mode and assert:

```ts
expect(screen.getByRole("heading", { name: "Редактирование интеграции" })).toBeTruthy()
expect((screen.getByLabelText("Адаптер") as HTMLSelectElement).disabled).toBe(true)
expect((screen.getByLabelText("Тестовое подключение") as HTMLInputElement).disabled).toBe(true)
expect((screen.getByLabelText("Название") as HTMLInputElement).value).toBe("Sandbox")
```

Change `Название`, `Начальный баланс, RUB` and the `token` field. Assert each interaction emits a new complete draft through `update:draft` and emits the corresponding `clear-field` path. Assert submit and cancel only emit events and perform no request.

- [x] **Step 2: Write failing component test for create mode**

Render the same component with an empty draft in create mode. Assert heading `Новая интеграция`, enabled adapter/contour controls, and:

```ts
await fireEvent.update(screen.getByLabelText("Адаптер"), "TINVEST_SANDBOX")
expect(emitted()["select-adapter"]).toEqual([["TINVEST_SANDBOX"]])
```

- [x] **Step 3: Run component tests and verify RED**

Run:

```bash
cd frontend && npm test -- --run src/components/BrokerForm.spec.ts
```

Expected: FAIL because `BrokerForm.vue` does not exist.

- [x] **Step 4: Implement the controlled form**

Implement `BrokerForm.vue` with `defineProps` and typed `defineEmits`. Use `:value`/`:checked` plus explicit event handlers rather than mutating the `draft` prop. Scalar updates emit:

```ts
emit("update:draft", { ...props.draft, [key]: value })
emit("clear-field", String(key))
```

Connection-field updates clone both the array and changed element:

```ts
const fields = props.draft.fields.map((field, itemIndex) =>
  itemIndex === index ? { ...field, value } : field,
)
emit("update:draft", { ...props.draft, fields })
emit("clear-field", `fields.${props.draft.fields[index].name}`)
```

Render `enabled` as an editable checkbox in both modes. Disable only adapter selection and `is_test` when `mode === "edit"`. Submit and cancel emit their events without preprocessing draft values.

- [x] **Step 5: Run component tests and verify GREEN**

Run:

```bash
cd frontend && npm test -- --run src/components/BrokerForm.spec.ts
```

Expected: all component tests pass.

- [x] **Step 6: Review checkpoint without commit**

Confirm the component imports only broker DTO types, contains no request code, has no required-field or numeric checks, and does not mutate props.

---

### Task 2: Broker-list create/edit orchestration

**Files:**

- Modify: `frontend/src/views/BrokersView.vue`
- Modify: `frontend/src/views/BrokersView.spec.ts`
- Read: `frontend/src/api/brokers.spec.ts`

**Interfaces:**

- Consumes `BrokerForm` and the existing API calls:

```ts
createBroker(draft: BrokerDraft): Promise<Broker>
replaceBroker(brokerId: string, draft: BrokerDraft): Promise<Broker>
```

- Produces view-local state:

```ts
const formMode = ref<"create" | "edit">("create")
const editingBrokerId = ref<string>()
```

- [x] **Step 1: Write failing edit-flow test**

Extend `BrokersView.spec.ts` with a broker returned from the initial `GET /api/brokers`. Click `Редактировать`, assert the prefilled reusable form is visible, edit mutable values, and submit. Verify the request exactly:

```ts
expect(fetchMock).toHaveBeenCalledWith("/api/brokers/broker-1", {
  method: "PUT",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(expectedCompleteDraft),
})
```

Mock the replacement response and both reload responses. Assert the form closes only after successful reload and the refreshed broker name is rendered.

- [x] **Step 2: Write failing cancel and error tests**

Test that cancel after changing the copied draft sends no `PUT`, closes the form and leaves the original broker row unchanged. Test a `422` `BrokerApiError` response for `fields.token`: the edit form remains open, the token control gets `aria-invalid="true"`, and the backend message is displayed.

- [x] **Step 3: Preserve the existing create-flow test**

Keep `renders backend field errors without frontend business validation` semantically unchanged. Its assertions must still prove that an empty create submit reaches Core and renders the returned `display_name` error rather than being blocked in the browser.

- [x] **Step 4: Run view tests and verify RED**

Run:

```bash
cd frontend && npm test -- --run src/views/BrokersView.spec.ts
```

Expected: new edit/cancel tests fail because the list has no edit action and the view does not use `replaceBroker`.

- [x] **Step 5: Implement explicit form lifecycle in `BrokersView`**

Import `BrokerForm`, `replaceBroker` and `Broker`. Replace `showForm` with explicit helpers:

```ts
function openCreate(): void
function openEdit(broker: Broker): void
function closeForm(): void
function copyDraft(broker: Broker): BrokerDraft
```

`copyDraft` copies scalars and maps `fields` to new objects. `openEdit` records `broker.id` and uses that copy. `save` chooses `replaceBroker(editingBrokerId.value, draft.value)` only in edit mode; otherwise it uses `createBroker`. Both success paths call `load()` before `closeForm()`. Failure paths leave the form open and map `BrokerApiError.fields` exactly as before.

- [x] **Step 6: Replace inline form markup with `BrokerForm`**

Add `Редактировать` to each broker row and wire the controlled component:

```vue
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
```

The `Добавить брокера` action calls `openCreate`; editing never calls `selectAdapter` because the selector is disabled.

- [x] **Step 7: Run focused frontend tests and verify GREEN**

Run:

```bash
cd frontend && npm test -- --run src/components/BrokerForm.spec.ts src/views/BrokersView.spec.ts src/api/brokers.spec.ts
```

Expected: reusable form, view orchestration and unchanged API contract tests all pass.

- [x] **Step 8: Review checkpoint without commit**

Classify failures. Changes required only for event/prop or DOM interface adaptation are valid. Any change that makes immutable identity editable, adds frontend validation, changes backend semantics or weakens an existing test condition requires separate user review.

---

### Task 3: Intermediate milestone and regression acceptance

**Files:**

- Modify: `docs/superpowers/plans/2026-08-10-decoupled-database-runtime.md`
- Test: `frontend/src/components/BrokerForm.spec.ts`
- Test: `frontend/src/views/BrokersView.spec.ts`
- Test: complete repository suites

**Interfaces:**

- Produces an explicit `0.4.5.1` intermediate UI checkpoint between the blocked Core live-cutover work and physical Worker-volume transition.
- Does not mark Task `0.4.5` or Worker-volume Steps `0.4.6.4-0.4.6.5` complete.

- [x] **Step 1: Record the intermediate checkpoint in the runtime plan**

Insert `Task 5.1 (0.4.5.1 MINOR): Editable broker settings UI` before Task 6. Link the accepted design and this implementation plan. State the DoD:

```text
- one reusable create/edit broker form;
- immutable broker identity in edit mode;
- complete PUT replacement through the existing contract;
- backend-owned validation with field-level display;
- focused frontend tests, build and full regression pass;
- no Core cutover or volume recreation in this task.
```

- [x] **Step 2: Apply frontend formatting and run complete frontend tests**

Run:

```bash
cd frontend && npm test -- --run
cd frontend && npm run build
```

Expected: all Vitest tests pass and the production TypeScript/Vite build exits `0`.

- [x] **Step 3: Run project format and lint gates**

Run:

```bash
uv run ruff check --config pyproject.toml --fix .
uv run black --config pyproject.toml .
uv run ruff check --config pyproject.toml src tests
```

Expected: formatting completes and Ruff reports no violations. Review the resulting diff to ensure formatters did not alter unrelated user work.

- [x] **Step 4: Run complete Python regression**

Run:

```bash
uv run pytest -q
```

Expected: all configured tests pass; PostgreSQL integration tests may skip only when `POSTGRES_TEST_DATABASE_URL` is absent, with an explicit skip reason.

- [x] **Step 5: Review acceptance without commit**

Verify the diff changes only the reusable broker form, broker-view orchestration, their tests and milestone documentation. Confirm no backend, database, migration, broker adapter or trading-strategy file changed. Record exact test/build counts in the runtime plan. Do not rebuild or recreate the running frontend container yet because current Compose topology is staged for the still-unaccepted Core cutover.

---

### Task 4: Selection and double-click-only broker edit entry

**Files:**

- Modify: `frontend/src/views/BrokersView.vue`
- Modify: `frontend/src/views/BrokersView.spec.ts`
- Modify: `docs/superpowers/specs/2026-08-11-broker-settings-edit-design.md`
- Modify: `docs/superpowers/plans/2026-08-10-decoupled-database-runtime.md`

**Interfaces:**

- Adds view state:

```ts
const selectedBrokerId = ref<string>()
```

- Adds view action:

```ts
function selectBroker(brokerId: string): void
```

- Preserves `openEdit(broker: Broker)`, `replaceBroker` and the reusable `BrokerForm` contract.

- [x] **Step 1: Write failing selection and double-click-only tests**

Change the existing edit-flow test so the production change that makes it fail is missing selection and double-click behavior. Assert there is no `Редактировать` button, click the broker row, then assert:

```ts
expect(brokerRow.classList.contains("data-table__row--selected")).toBe(true)
```

Assert the single click did not open the form, double-click the row, and keep the existing complete `PUT /api/brokers/broker-1` assertions unchanged.

- [x] **Step 2: Write failing double-click and missing-selection tests**

Add one test that double-clicks an unselected broker row and asserts both row highlighting and the open `Редактирование интеграции` form. Add one test whose delete response reloads an empty broker list; after selecting and deleting that broker, assert no selected row remains. All edit-flow tests assert that no `Редактировать` button exists.

- [x] **Step 3: Run the view suite and verify RED**

Run:

```bash
cd frontend && npm test -- --run src/views/BrokersView.spec.ts
```

Expected: selection tests fail because editing is still a per-row action and broker rows have no selection state or double-click handler.

- [x] **Step 4: Implement selection state and reconciliation**

Add `selectedBrokerId` and make `load()` assign its two responses before reconciling selection:

```ts
const [settings, currentEnvironment] = await Promise.all([
  fetchBrokerSettings(),
  fetchEnvironment(),
])
data.value = settings
environment.value = currentEnvironment
if (selectedBrokerId.value && !settings.brokers.some((broker) => broker.id === selectedBrokerId.value)) {
  selectedBrokerId.value = undefined
}
```

`selectBroker` assigns the ID. `openEdit` also selects its broker so double click has one atomic behavior. `closeForm` clears only the editing target and preserves page selection.

- [x] **Step 5: Replace per-row edit with double-click-only action**

Do not add a page-level edit button and remove the row edit button. Bind each broker article:

```vue
<article
  :class="{ 'data-table__row--selected': selectedBrokerId === broker.id }"
  tabindex="0"
  @click="selectBroker(broker.id)"
  @dblclick="openEdit(broker)"
  @keydown.enter.prevent="selectBroker(broker.id)"
>
```

Add `@click.stop` to row action buttons and `@dblclick.stop` to their action container so checking, provisioning, navigating or deleting does not accidentally open the edit form.

- [x] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
cd frontend && npm test -- --run src/views/BrokersView.spec.ts src/components/BrokerForm.spec.ts src/api/brokers.spec.ts
cd frontend && npm run typecheck
```

Expected: selection, double-click-only edit, cancel/error, component, API and type contracts pass.

- [x] **Step 7: Run full acceptance and update checkpoint counts**

Run from their stated working directories:

```bash
cd frontend && npm test -- --run && npm run build
uv run ruff check --config pyproject.toml src tests
uv run pytest -q
```

The two Python commands run from repository root, not `frontend/`. Expected: frontend suite/build, Ruff and Python regression pass; only externally configured PostgreSQL tests may skip. Update Task `0.4.5.1` with the new exact frontend count and note that no runtime container or volume was recreated.

- [x] **Step 8: Review checkpoint without commit**

Confirm the page and row have no edit button, a single click only selects, a double click opens editing, cancel preserves selection, deletion clears a missing selection, no frontend validation was added, and no backend or trading file changed.

Checkpoint completed on 2026-08-11. Focused form/view/API acceptance reports
`16 passed`; complete frontend acceptance reports `74 passed`; Vue TypeScript
typecheck and production Vite build exit `0`. Ruff is clean and the global
Python regression reports `614 passed, 4 skipped`. The four skips require the
external `POSTGRES_TEST_DATABASE_URL`. No container or volume was recreated.
