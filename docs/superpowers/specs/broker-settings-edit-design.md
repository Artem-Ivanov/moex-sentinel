# Broker Settings Edit Design

## Context

The brokers page already lists configured integrations and contains the form
used to create a broker. Core already exposes `PUT /api/brokers/{broker_id}` and
the frontend API client already provides `replaceBroker`. The missing part is a
UI flow that opens the existing broker form with the selected settings and
sends the replacement request.

This feature is an intermediate step before the live Core database cutover. It
does not change broker persistence, migration behavior, trading strategy or
runtime lifecycle.

## User intent

From the brokers list, the user selects an existing broker, opens its mutable
settings using the same form as broker creation, saves the replacement, or
cancels without changing persisted data.

## Scope

### Mutable settings

- display name;
- enabled state;
- initial sandbox balance;
- adapter-defined connection fields, including FQDN and token.

### Immutable identity

- adapter code;
- provider code;
- environment code;
- test/production contour flag.

Changing immutable identity means creating another broker integration and is
not part of editing.

### Out of scope

- a separate broker-details or edit route;
- frontend business validation;
- masked or encrypted connection fields;
- changes to the backend replace contract;
- changes to sandbox provisioning state;
- changes to database migration or trading behavior.

## Component design

Create `frontend/src/components/BrokerForm.vue` and move the current creation
form markup into it. The component supports two explicit modes:

- `create`: empty draft, adapter selector enabled, heading `Новая интеграция`;
- `edit`: draft copied from the selected broker, adapter selector and contour
  flag disabled, heading `Редактирование интеграции`.

The component renders fields and emits user intent. It does not call the API,
normalize values or implement business validation. Its inputs are:

- mode;
- current draft;
- adapter definitions;
- backend field errors.

Its outputs are:

- draft updates;
- submit;
- cancel;
- adapter selection in create mode;
- notification that an edited field changed so the parent can clear the
  corresponding backend error.

`BrokersView.vue` remains the orchestration view. It owns the active form mode,
selected broker ID, editing broker ID, draft, errors and API calls. The selected
ID controls the highlighted list row; the editing ID identifies the immutable
target of an open edit form.

## User flow

### Create

1. The user presses `Добавить брокера`.
2. The view resets the draft and opens `BrokerForm` in create mode.
3. Selecting an adapter populates its code-owned provider, environment and
   connection-field definitions.
4. Submit calls `createBroker`.

### Edit

1. A single click selects and highlights one broker row.
2. A single click does not open the form. Editing has no separate button.
3. Double-clicking a broker row opens the edit form and also makes that row the
   current selection.
4. The view copies all returned broker settings into a new draft and opens the
   same `BrokerForm` in edit mode.
5. Adapter identity and contour controls remain visible but disabled.
6. Submit calls `replaceBroker(editingBrokerId, draft)`; the editing ID is set
   from the selected row when the double-click opens the form.
7. On success the view reloads the brokers list, clears the form state and
   closes the form.

The page and broker row contain no separate edit button. Existing per-broker
actions such as connection check, sandbox preparation, accounts and deletion
remain in the row. If reload or deletion removes the selected broker, the
selection is cleared.

### Cancel

Cancel clears the editing broker ID, backend errors and draft, then closes the
form. It performs no request and preserves the page selection, so the user may
open the same broker again without selecting its row a second time.

## Data and mutation rules

The edit draft must be a deep copy of the broker returned by Core. Editing form
fields must never mutate the list item before a successful response.

The complete `BrokerDraft` is sent to the existing replacement endpoint. The
frontend does not construct a partial update and does not infer omitted values.
The backend remains the sole owner of required-field, adapter, environment,
FQDN and numeric validation.

## Error handling

`BrokerApiError.fields` is mapped to the same form paths used during creation.
The form highlights the corresponding control and renders the backend message.
Editing a field clears only that field's previous error. A non-field request
failure is shown as the page-level error and leaves the edit form open.

Immutable identity received from the backend is still sent unchanged. If Core
rejects it, the returned backend error is displayed; the frontend does not
silently repair the payload.

## Tests and acceptance

Frontend component/view tests verify that:

1. the page and broker row have no `Редактировать` action;
2. a single click selects and highlights a broker without opening the form;
3. a double click selects the row and opens the reusable form with a copied
   broker draft;
4. adapter identity and contour controls are disabled in edit mode;
5. mutable settings remain editable;
6. submit sends the unchanged complete draft through `PUT /api/brokers/{id}`;
7. backend field errors are highlighted without frontend business validation;
8. successful replacement reloads the list and closes the form;
9. cancel sends no request and does not mutate the broker row;
10. missing or deleted broker data clears the selection;
11. creation continues to use the same form and `POST /api/brokers`.

No new backend endpoint or backend behavior is required. Acceptance includes
the focused frontend suite followed by the complete project regression. Test
failures that require changing the feature requirements are not accepted as
interface adaptations and must be reviewed separately.
