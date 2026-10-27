# PROD Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan task-by-task. Track each step with checkbox syntax and request an independent review at every package handoff.

**Goal:** Закрыть согласованные PROD-пробелы из спецификации, сохранив TEST/Sandbox и проверив путь Analytics → production decision → SDK → Core PostgreSQL без реальной заявки.

**Architecture:** Core остаётся владельцем admission и финансовых фактов; Worker — durable execution и broker RPC; Analytics — market-only. Три независимых пакета Core, Worker lifecycle и diagnostics/UI реализуются параллельно в заданных границах, затем root интегрирует их отдельной PostgreSQL проверкой и ведёт безопасную подготовку deployment.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, SQLAlchemy/PostgreSQL, Worker SQLite, `t-tech-investments` 1.49.3.x, Vue 3/Vitest, pytest.

**Spec:** [2026-10-17-prod-completeness-design.md](../specs/2026-10-17-prod-completeness-design.md)

## Global Constraints

- Сохранять существующие TEST/Sandbox, mocks и уже имеющиеся PROD flow/ingress integration cases.
- Core остаётся единственным владельцем PostgreSQL/admission/facts; Worker — SQLite/durable execution/broker submit-cancel; Analytics не получает credentials/portfolio.
- Для operator mutations проверять scope и transactional `broker.environment`; TEST→PROD отказ не мутирует данные и не даёт HTTP 500.
- API trade availability получать только из read-only unary `GetTradingStatusResponse`; stream не подтверждает и не освежает этот flag. Unknown/stale закрывает eligibility; `False` — обычная недоступность.
- Control heartbeat не означает broker readiness. Нет diagnostics → `UNKNOWN`; до первой команды — `IDLE`; permanent broker block виден как DEGRADED и не меняет Core DB/schema health на DOWN.
- Deadline = durable `dispatch_started_at + order_ttl_seconds`, UTC milliseconds, истечение на `now >= deadline`; restart/ENV не продлевают его. Legacy intent без надёжного snapshot/timestamp не получает TTL из ENV и остаётся fail-closed.
- Не отменять terminal order; cancel только после свежего nonterminal status и при текущих owner/generation, действующей lease/fencing, TRADE. ACK не terminal proof. Lost submit ACK/`not found` не разрешает повтор submit.
- Active intent и полный reservation удерживаются до атомарной финализации/outbox commit; после commit снимается gate и освобождается reservation, durable intent/финансовые факты сохраняются. Partial fills учитываются по фактическим цене и комиссии.
- Не переводить HOLD в IN_WORK автоматически; сохранять UNCERTAIN и ручной HOLD. Не делать blind replacement, retry chain или repricing по старому решению.
- Не менять приватный ENV, caps, ARM/Resume/HOLD вручную, не терять volumes. Не выполнять PROD SQL/SQLite inspection, SSH/live broker mutation или торговые действия вне отдельно заданного rollout gate.
- PostgreSQL integration tests требуют отдельный тестовый экземпляр через `POSTGRES_TEST_DATABASE_URL`; схема `test_<uuid>` внутри неизвестной/рабочей БД не считается изоляцией. Worker tests используют SQLite `tmp_path`.
- Любую необходимую миграцию сопровождать проверяемым downgrade на отдельной тестовой БД; не добавлять миграцию без необходимости.
- Ветка/worktree, stage, commit и push запрещены без отдельного распоряжения; локальные изменения пользователя сохранять.

## Review Focus

- **Смешение контуров на отложенной operator mutation:** тесты Core должны проверять TEST operator→PROD row для всех operator methods, включая pending runtime, с отсутствием мутации и 500.
- **Старая или ложная API availability:** Core market tests должны проверять missing/stale unary result и stream event после unary result; последняя не меняет его observed_at/freshness, eligibility закрыта до свежего подтверждения.
- **Control progress при permanent broker failure:** Worker/Core/UI tests должны показывать live heartbeat вместе с явной permanent-block причиной/DEGRADED; `IDLE`, `UNKNOWN`, Analytics `None` и намеренные HOLD/READ_ONLY/disabled остаются различными состояниями.
- **Race между cancel, fills, timeout и restart:** Worker tests должны проверять точную TTL границу, cancel denial при stale owner/generation/expired lease/READ_ONLY, partial fill, lost ACK, terminal query и сохранение полной reservation до atomic commit.
- **Смешанная версия protocol и повторная отправка:** contract/integration tests должны проверять старый diagnostics payload как UNKNOWN, commit/outbox replay без дубликата, lost submit ACK + `not found`, legacy intent без TTL из нового ENV и сохранение HOLD.

---

## File ownership and dependency order

| Package | Owner | Exclusive implementation files | Dependency |
|---|---|---|---|
| PC1 Core isolation + truthful market flag | Senior Core | `src/moex_sentinel/composition.py`; `services/trading_admission.py`, `services/trading_admission_ports.py`; `storage/repositories/trading_admission.py`; `views/trading_admission.py`; `adapters/tinvest/streaming.py`, `adapters/tinvest/market_stream_source.py`; relevant market snapshot gateway/domain/state files; `src/sentinel_contracts/streaming_market.py`; corresponding Core unit/API/PG tests | Independent; no PC3 files |
| PC2 Worker TTL/cancel/recovery | Senior Worker | `src/trading_automaton/composition.py`; `services/order_tracking.py`, `order_dispatch.py`, `uncertain_intent_reconciliation.py`, `broker_tick_preparation.py`; narrowly scoped lifecycle module/ports/DTOs only if existing seams cannot express contract; `adapters/tinvest_broker_session.py`; `config.py`; corresponding Worker tests | Independent; first verify pinned SDK contract; no PC3 runtime/usecase files |
| PC3 Diagnostics/startup alert/UI | Senior diagnostics | `src/sentinel_contracts/worker_diagnostics.py`; `src/trading_automaton/runtime/analytics_broker.py`, `runtime/streaming_coordinator.py`, `usecases/broker_iteration.py`; `src/moex_sentinel/services/runtime_versions.py`, `services/trading_readiness_alerts.py`, `api/app.py` expected-work scoped wiring; `frontend/src/api/diagnostics.ts`, its spec; `frontend/src/views/DiagnosticsView.vue` and spec; diagnostics tests | May run alongside PC1/PC2 only within listed exclusive ownership; no composition file until PC1/PC2 handoff |
| PC4 PROD composition integration | Root coordinates/accepts; separately delegated senior integration-test implementer writes | `tests/integration/postgresql/test_prod_decision_pipeline.py`; implementer owns test-local/new fixtures if required | After PC1–PC3 interfaces settle |
| PC5 deployment preparation | Root coordinates; if helper code is needed, a separately delegated implementer owns the helper | No source ownership presumed. Before any helper is written, root records its exact path, immutable inputs, safe evidence source and assigns an implementer. | After all implementation/reviews and package identity verification |

Shared contracts are handed off by their package owner. Do not edit another package's owned files to resolve a conflict; report the conflict to root. Root owns the overall plan, final integration, deployment decision/evidence, and project context updates.

## Task 1: PC1 — Core operator isolation and unary trading availability

**Files:** Ownership list PC1, plus existing tests `tests/services/test_trading_admission.py`, `tests/api/test_trading_admission_limits.py`, `tests/api/test_pending_runtime_candidate.py`, `tests/trading_automaton/adapters/test_tinvest_streaming_adapter.py`, `tests/adapters/test_tinvest_market_stream_source.py`, `tests/services/test_market_snapshot_gateway.py`, `tests/integration/postgresql/test_trading_admission_flow.py` (or the narrowest existing PG owner test). Find the precise market state path before editing; do not move responsibilities across Core/Worker.

**Interfaces:** Consume existing `AutomatonBrokerService.scope` broker/account/environment semantics and existing HTTP operator auth. Composition injects pinned `settings.application_environment`; repository transaction verifies `broker.environment` before mutations. Market flag uses existing market-data source/gateway and `GetTradingStatusResponse`; stream contract does not carry the API flag as an observation. Preserve final Core permit risk check.

- [x] Write failing operator tests for TEST operator attempting PROD command and pending-runtime mutation: expect mapped environment refusal, unchanged rows, no 500. Add direct repository transactional guard case.
- [x] Run focused tests: `uv run --python 3.12 pytest tests/services/test_trading_admission.py tests/api/test_trading_admission_limits.py tests/api/test_pending_runtime_candidate.py -q`. Expect new cross-environment cases to fail before implementation.
- [x] Implement pinned environment injection and transactional `broker.environment` guard; preserve operator HTTP auth and internal Worker scope.
- [x] Write failing streaming tests proving stream TradingStatus cannot supply/refresh API flag; test unary true/false, missing, stale and decision eligibility.
- [x] Run focused market tests: `uv run --python 3.12 pytest tests/trading_automaton/adapters/test_tinvest_streaming_adapter.py tests/adapters/test_tinvest_market_stream_source.py tests/services/test_market_snapshot_gateway.py -q`. Expect flag/source cases to fail before implementation.
- [x] Route read-only unary trading status through existing bounded source/gateway; maintain separate observed_at/expiry; remove stream literal `True`; keep `False` as normal unavailable and keep Core permit recheck.
- [x] Run focused Core unit/API tests and existing production admission PG flow with a verified isolated test DB: `uv run --python 3.12 pytest tests/integration/postgresql/test_trading_admission_flow.py -q`. No PG URL or shared/production URL means stop that PG check and report blocker; do not count a skip as acceptance.
- [x] Deliver diff and evidence to independent Core reviewer. Fix/re-review Critical and Important findings before handoff.

## Task 2: PC2 — Worker TTL, cancel and recovery lifecycle

**Files:** Ownership list PC2; corresponding tests include `tests/trading_automaton/services/test_order_tracking_service.py`, `test_order_dispatch_service.py`, `test_uncertain_intent_reconciliation_service.py`, `test_broker_tick_preparation_service.py`, `tests/trading_automaton/adapters/test_tinvest_broker_session.py`, `tests/trading_automaton/test_config.py`, plus relevant local repository/state tests for restart/replay.

**Interfaces:** Use existing durable intent snapshot and `dispatch_started_at`; use existing broker session and status/order tracking seams. Keep exactly one lifecycle per intent. If a new focused lifecycle module is essential, PC2 owns its narrow DTO/port and records exact names/types in handoff before PC4 consumes it. No PC2 changes to `runtime/analytics_broker.py`, `runtime/streaming_coordinator.py`, or `usecases/broker_iteration.py`.

- [x] Before cancel implementation, inspect the installed/locked `t-tech-investments` 1.49.3.x SDK and verify the actual cancel method signature and accepted order-id fields using its installed package metadata/signature. Do not infer SDK keyword shape from online docs. If pinned SDK cannot express the required scoped cancel, stop this dependent behavior and report the compatibility issue to root; do not silently change dependency.
- [x] Add failing Worker tests for deadline at `now == deadline` and one instant before/after; restart does not extend deadline; missing timestamp/snapshot legacy intent does not take TTL from new ENV and stays fail-closed.
- [x] Run: `uv run --python 3.12 pytest tests/trading_automaton/services/test_order_tracking_service.py tests/trading_automaton/services/test_order_dispatch_service.py tests/trading_automaton/services/test_uncertain_intent_reconciliation_service.py tests/trading_automaton/services/test_broker_tick_preparation_service.py -q`. Expect new lifecycle assertions to fail.
- [x] Implement durable deadline calculation from `dispatch_started_at + order_ttl_seconds`, UTC milliseconds, with `now >= deadline`; keep ENV and restart from changing existing intent deadline. Deprecate unused `STRATEGY_ORDER_RETRY_LIMIT` transparently while retaining config compatibility and historical snapshot decoding.
- [x] Add failing cancel/recovery cases: fresh status skips terminal; nonterminal cancellation succeeds only under TRADE/current owner/generation/fencing/unexpired lease; reject stale owner, stale generation, expired lease and READ_ONLY; cancel timeout/lost ACK queries same order; repeat cancel only after fresh same-order nonterminal proof and cooldown.
- [x] Add failure/race cases: cancel ACK alone does not finalize; fill and partial fill concurrent with cancel retain actual price/commission; submit lost ACK lookup by request ID and not-found never reposts; portfolio outage does not block scoped order query/cancel but blocks fill finalization only when its existing validation needs portfolio facts.
- [x] Implement cancellation/reconciliation using verified SDK contract. Preserve full active intent/reservation until atomic local ledger + execution/outbox commit; on unknown proof set UNCERTAIN/HOLD; preserve manual HOLD; after confirmed cancel allow only a later independent fresh decision with new permit. No blind replacement/reprice/retry chain.
- [x] Run focused lifecycle, SDK adapter and state replay tests on Python 3.12 and file SQLite `tmp_path`; include existing worker replay tests. Check `uv run --python 3.12 pytest tests/trading_automaton/storage/test_local_repository.py tests/trading_automaton/storage/test_decision_audit_outbox.py -q`.
- [x] Deliver diff, verified SDK signature evidence (no secrets), and test evidence to independent Worker reviewer. Fix/re-review Critical and Important findings before handoff.

## Task 3: PC3 — Broker diagnostics, startup alert and UI

**Files:** Ownership list PC3; tests `tests/contracts/test_worker_diagnostics.py`, `tests/services/test_runtime_versions.py`, `tests/api/test_runtime_versions.py`, `tests/services/test_trading_readiness_alerts.py`, `tests/api/test_runtime_environment_wiring.py`, `tests/trading_automaton/test_analytics_runtime.py`, `tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py`, `tests/usecases/test_diagnostics.py`, `tests/api/test_diagnostics.py`; frontend `frontend/src/api/diagnostics.spec.ts`, `frontend/src/views/DiagnosticsView.spec.ts`.

**Interfaces:** Worker diagnostics is volatile observation, separate from durable lifecycle. No diagnostics means Core `UNKNOWN`, no command means `IDLE`; permanent broker block carries a safe reason. Existing heartbeat→WorkerVersionStore→diagnostics→Telegram queue remains the delivery path. Core health/schema and worker control progress retain their independent meanings.

- [x] Add failing contract/Core/Worker tests for `IDLE`, missing/stale payload→`UNKNOWN`, permanent broker failure while heartbeat/control progresses→DEGRADED with safe reason, and Analytics `None` continuing recovery preparation.
- [x] Run focused tests: `uv run --python 3.12 pytest tests/contracts/test_worker_diagnostics.py tests/services/test_runtime_versions.py tests/api/test_runtime_versions.py tests/trading_automaton/test_analytics_runtime.py tests/trading_automaton/services/test_streaming_runtime_coordinator_service.py tests/usecases/test_diagnostics.py tests/api/test_diagnostics.py -q`. Expect new diagnostics assertions to fail.
- [x] Extend diagnostics contract and Worker publication/Core observation so omitted old fields become UNKNOWN, never silent green; verify safe reason filtering and that broker block does not mark Core DB/schema health DOWN.
- [x] Add failing alert tests for sustained startup block gated by scoped IN_WORK + TRADE + enabled + allowlisted reason, grace/confirmation/dedup/bounded queue; prove HOLD, READ_ONLY and disabled modes produce no alert.
- [x] Run alert and wiring tests, then implement only scoped expected-work wiring in `api/app.py`; do not change credentials/private ENV or add a manual permit.
- [x] Add failing UI contract/view tests distinguishing broker readiness/block from heartbeat/control progress, with UNKNOWN and IDLE display.
- [x] Run `cd frontend && npm test -- src/api/diagnostics.spec.ts src/views/DiagnosticsView.spec.ts`, `npm run typecheck`, and `npm run build`; implement the smallest view/API update that passes.
- [x] Before release, prove mixed-version compatibility: Core/UI with old payload show UNKNOWN; new Worker data is recognized only after compatible reader rollout. Coordinate shared contract handoff with PC1/PC2 without editing their composition files.
- [x] Deliver diff and evidence to independent diagnostics reviewer. Fix/re-review Critical and Important findings before handoff.

## Task 4: PC4 — Full production-composition integration test (root-coordinated, delegated implementation)

**Files:** Create only `tests/integration/postgresql/test_prod_decision_pipeline.py`; create a test-local fixture in that file or a new test-only fixture file owned by the delegated implementer if necessary. Reuse `tests/integration/postgresql/test_trading_admission_flow.py` and `test_trading_admission_ingress.py`; do not mirror their permit/tracking/replay/settlement/lost-ACK/transfer coverage.

**Interfaces:** Root assigns a senior integration-test implementer who did not implement PC1–PC3; root coordinates seams and accepts the result but does not edit the test. The implementer consumes handed-off PC1 market/API contracts and PC2 decision/SDK boundary. The test exercises Analytics HTTP → real production decision composition/risk/admission → fake SDK transport boundary → authenticated Core HTTP/PostgreSQL. No internal risk/admission layer is faked and no live broker order is sent. Root records actual seams and fixture setup from completed handoffs; do not invent a parallel production API.

- [x] Configure a dedicated PostgreSQL test instance and set `POSTGRES_TEST_DATABASE_URL` to that test instance; verify host/database identity from test configuration, not just generated schema name. Worker storage uses file SQLite under pytest `tmp_path`.
- [x] Add the composed happy-path coverage test: Analytics HTTP produces a fresh real production decision; actual decision/risk/admission runs; fake SDK transport returns a synthetic successful submit followed by a terminal fill with known actual quantity, price and commission. Assert exactly one durable terminal Worker intent/execution and the required complete ordered typed-fact outbox batch, publish all facts through authenticated Core HTTP and receive their ACKs, then assert exactly one confirmed Core order and one execution whose quantity, price and commission equal the synthetic broker fill. The SDK transport is fake; decision, risk, admission, Worker persistence/outbox and Core HTTP/PG are production composition.
- [x] Run the composed test against the completed package seams. This is coverage of existing composition; fixture/setup failures are not behavior RED evidence. Feature fixes have their separate behavior regressions.
- [x] Reuse existing coverage for lost ACK/recovery/transfer; add only uncovered interaction assertions required by this composed path. The composed happy path does not replace or duplicate those suites.
- [x] Run `uv run --python 3.12 pytest tests/integration/postgresql/test_prod_decision_pipeline.py tests/integration/postgresql/test_trading_admission_flow.py tests/integration/postgresql/test_trading_admission_ingress.py -q`. All targeted PG tests must execute against the separate test instance and pass; skip due to missing URL is a blocker, not a pass.
- [x] Root reviews final integration diff and asks an independent reviewer who did not implement PC4 to inspect full boundary, isolation, and duplicate-order behavior. Resolve Critical/Important findings and repeat affected tests.

## Task 5: PC5 — Reviewed package and constrained deployment preparation (root-coordinated)

**Files:** Root coordinates; no helper is presumed necessary. If concrete safe resolution requires helper code, root first records exact path, immutable inputs and evidence source, then delegates that file to an implementer. Root reviews/accepts it and retains ownership of deployment gates.

**Interfaces:** Input is the approved spec, this reviewed plan, completed PC1–PC4 source, independent C0/I0 reviews, test evidence, and immutable source/package identity. Deployment is downstream of user authorization already provided. Build/rollout helper must consume only reviewed inputs and safe application-level evidence; no database file or table inspection in PROD.

- [x] Root defines a source manifest tied to exact immutable Core/Worker/frontend source and package/image digests; verify package contents equal reviewed source and mixed rollout order reads new protocol before Worker publishes it.
- [x] Preserve known UNCERTAIN intent. Resolve the stale receipt prerequisite through a safe, reviewed application-level freshness evidence source before any deployment mutation. If no safe evidence source is available, stop deployment and report the concrete blocker; do not read PROD SQL/SQLite or fabricate freshness.
- [x] Confirm no private ENV/caps/ARM/Resume/HOLD/order edits, schema change, volume replacement, or destructive rollback is in the release. If a migration is strictly needed, root first demonstrates the need and verifies downgrade on separate test DB; never use rollback to delete finance or new operations.
- [x] Independent deployment-package reviewer checks immutable identity, compatibility order, preservation of UNCERTAIN/HOLD/volumes and exact bounded action set. Resolve Critical/Important findings before deployment.
- [x] Deploy only the reviewed package under existing user authorization and resolved safe evidence gate. Post-check only service health checks, pings and opening UI with basic functionality. Do not expand into production DB/SQLite integrity reads or broad data comparisons.
- [x] Record package digests, review findings/outcomes, allowed action evidence and limited post-deploy checks in the root-owned project plan/context. No agent commits, stages or pushes.

## Dependency graph and release sequence

```text
PC1 Core ──────┐
PC2 Worker ────┼──> PC4 composed PG integration ──> root final review ──> PC5 package/deployment gates
PC3 Diagnostics/UI ─┘
```

PC1 and PC2 may run concurrently. PC3 may run concurrently with both only inside its exclusive file list; it waits for their composition changes before any shared contract release. Each package uses red→green tests and independent reviewer handoff. PC4 begins after all three package interfaces and reviews are complete. Root performs final integration and obtains independent C0/I0 review across the combined diff. All Critical and Important findings must be fixed and independently rechecked before deployment. If the SDK signature, isolated PostgreSQL instance, or safe freshness evidence cannot be verified, block only the dependent step and keep safe completed work.

## Final acceptance

- PC1–PC4 targeted Python 3.12 checks pass, including meaningful isolated PostgreSQL integration with no blanket skip; frontend tests/typecheck/build pass.
- Ruff/Black (configured project versions) pass for changed Python files; applicable frontend lint/type/build checks pass. No new dependency or migration without explicit need and review.
- TEST/Sandbox/mocks and existing PROD flow/ingress coverage remain intact.
- Independent per-package reviews and final independent integration review report no unresolved Critical/Important findings; root records reviewer, reviewed scope, result, and limits.
- The package is immutable and exactly matches reviewed source. Rollout evidence and bounded post-checks satisfy PC5; otherwise deployment remains not accepted and the blocker is recorded.

## Historical intermediate checkpoint — 18.10.2026, before final acceptance

PC1 accepted: independent `prod_core_review` C0/I0/M0; focused market/admission checks and isolated PostgreSQL cross-contour case passed. PC3 accepted: independent `prod_diagnostics_review` C0/I0/M0; root frontend 259 tests, typecheck and production build passed. These reviews cover source/tests, not current VPS state. Evidence: `develop/reports/prod-completeness-20261018/pc1/verification.md`, `pc3/implementation.md`, `frontend-full-tests.log`.

At this intermediate checkpoint PC2 remained open: repeat `prod_lifecycle_review` C0/I2/M0 found a polling window exhausted before ordinary TTL cancellation and a scheduled-SDK-entry permit expiry race. Both are assigned to the author with genuine behavior regressions and mandatory independent recheck. PC4 new composed happy path passed with ingress (4 tests); independent integration review pending. Existing flow fixture leaves Core automation IN_QUEUE while expecting active admission; the same arrangement is present in HEAD, and its correction is assigned separately before the full suite. At that time PC5 helper review and deployment were still open; the final result below supersedes this checkpoint.

Use the prepared `.venv/bin/python` (3.12) and explicit `BROKER_ACCESS_MODE=READ_ONLY` for test collection; the PROD test constructs TRADE settings explicitly. PostgreSQL checks use only the dedicated tmpfs test instance at localhost:55434, database `sentinel_prod_completeness_test`. Do not run `uv sync` concurrently with checks. No PROD DB inspection, private ENV edits, caps, ARM/Resume/HOLD or financial operations are included.

## Historical corrective checkpoint — 18.10, before fixes and final acceptance

The first root Python run exercised the dedicated PostgreSQL instance: 2774 passed, 21 failed, 2 skipped (only paired restore rehearsal requires absent pg_dump). The temporarily owned flow file was checked separately. Market integration fixtures were still using the former stream API-trading flag; a shared full-snapshot unary fixture update and canonical ACCOUNT concurrency fixtures passed independent review `prod_integration_review` C0/I0/M0 and 65 tests. Safety, stale-market, HOLD, lost-ACK, one-submit and post-lock clock assertions remain covered.

A real source gap was also found: a new trusted ACCOUNT reconciliation leaves SETTLED reservations bound to the old position anchor, so later normal submit can be denied despite complete current risk data. Corrective ownership is Core repository/service and the existing service/PG flow tests. Preserve financial values, idempotency and history; rotate only anchor bookkeeping atomically under the account lock and preserve strict checks for every unresolved reservation. Normal grant settlement and the next whole-account anchor promotion occur in the same transaction, so committed SETTLED history is already rebased and has zero remaining delta. Verify this through the public service flow; a test that commits the private settlement helper alone does not represent that production invariant. Closed historical UIDs must not require a current instrument/position anchor when their fills are already included. Test through normal service authorization, not only direct repository calls; include positive fills, repeated rotations, closed UIDs, malformed execution data, and stale unresolved anchors. Independent financial re-review is mandatory before the final full suite and package. No production data is edited to implement this fix.

PC5 initial source review found executable wiring defects despite pure-function tests passing. Acceptance now includes tests that call actual RemoteOps methods and the CLI with synthetic Docker/HTTP/filesystem inputs. At this historical checkpoint the outstanding checks covered bounded first heartbeat after transfer, physical RW mount overlap across volume/bind and ancestor paths, and exact candidate tag-to-reviewed-image binding before the old Worker stops. Narrow reader SHA582c59db… was separately accepted and executed for supported API/container metadata only; this does not accept rollout.

## Final result — deployed 18.10.2026

Root final full backend on Python3.12/dedicated PostgreSQL:2804PASS,2SKIP only absentpg_dump pairedrestore rehearsal; frontend259PASS/typecheck/buildPASS, Ruff/Black/diffcheckPASS. All product/financial/integration/helper/exactpackage and final deployment reviews C0/I0/M0. The new composed integration is coverage-only: no source behavior RED is claimed for fixture construction; financial/lifecycle fixes have independently checked behavior regressions.

`vps-20261018-prod-437cc0757b90` deployed at09:02:08UTC: four images, clean oldWorker exit0, compatible readers before Worker, one owner transfer to generation6, then healthy candidate heartbeat/health/UI assets. Public HTTPS health and fresh-profile rendered login passed. No DDL, privateENV/caps/volume edits or HOLD/Resume. Valid acceptance receipt in `develop/reports/prod-completeness-20261018/acceptance-receipt.json`; execution trace, package receipts, smoke artifacts and verification.json in the same directory.

Limits were null at transfer, so new submits remained blocked; actual trading and current exact intent were not accepted. Historical UNCERTAIN keeps17.10date/ref and HISTORICAL_NOT_RECHECKED. Additional post-rollout API bundles remained unavailable through unstable SSH transport; the optional read-only attempt was cancelled. This does not replace or weaken the already passed limited deployment acceptance. No blind retry of owner POST occurred.
