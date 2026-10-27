# F1/F2 Implementation Plan

> For agentic workers: superpowers:subagent-driven-development; existing checkout only, no branch/worktree/commit/stage/push.

**Goal:** учесть поздние комиссии идемпотентно и ускорить Worker D1 при сохранении100ms.
**Architecture:** Core observes exact broker fee parent and keeps append-only financial overlay; Worker consumes a bounded derived effective-entry-fee cache through existing statuses. Existing portfolio process drives fair bounded history scan.
**Tech Stack:** Python/FastAPI/SQLAlchemy/PostgreSQL/Alembic/WorkerSQLite, no new dependencies.
**Spec:** docs/superpowers/specs/2026-10-27-execution-commissions.md.

## Constraints / review focus
Preserve raw facts, immutable accountscope, late/partialfills, duplicate/cursor/restart safety and providerIDsemantics. No SDK under DB lock, no fixedhistorical24h cutoff, no doubledebit account summary/daily loss. Optionalwirefields finite/scoped/defaultcompatible; unknown explicit/auto transient no manualARM. Migration/rollbackonisolatedDB; working volumes unchanged.

## Task F1 — integrated financial package (single writer, P1 ACK received)
Files new: Core domain/execution_commissions.py, storage/models/execution_commissions.py, storage/repositories/execution_commissions.py, services/execution_commissions.py; alembic/versions/0008_execution_commissions.py. Existing Core: domain/portfolio.py, adapters/tinvest/portfolio.py, storage/models/__init__.py, storage/repositories/automation_commands.py, automations.py, order_facts.py, composition.py, portfolio_snapshot_worker.py, usecases/trading_fact_ingress.py, views/internal_trading_facts.py, views/schemas/trading_facts.py; domain/automations.py orrecord/schemafinancialstatus onlyifnecessaryafterownerproposal. Shared: sentinel_contracts/trading_facts.py. Worker: storage/models.py,database.py,repository.py,usecases/synchronize_runtime.py,adapters/core_client.py,composition.py,services/fact_synchronization.py and domain/core_contracts.py ifactualwrapperneedsit; services/position_state_hydration.py for post-reconcile decision-only overlay. Raw list_open_lots/intent_history unchanged. NO F2 reader/test edits.
Tests new Core services/storage/PG execution_commissions; migration schema test; existingcontract/statusAPI/CoreClient/Workerrepo/sync/decision/portfolio-worker tests asneeded. Owner must preservebefore-edit snapshots/hashes; return exactdecomposition/remainingfileowners and rootACK beforeediting.

- [ ] Reproduce terminalfee0 + scopedbrokerfee3.58 using isolatedfakeSDK/DB, no livefinancialtest.
- [ ] Domainnormalization/sourcevalidation/pagefairness/idempotency testedRED thenminimalcode.
- [ ] Schema models/alembicupgrade+emptydowngrade/nonemptyreject andWorkerinit/downgradeisolatedtests.
- [ ] Coreingestion+sourcecursor+effectivePnL all open/closed fractions/remainingcases in spec.
- [ ] Internalstatusesbounded exactrequestlookup+Workerderivedfaircache+decisionreader overlay, no rawfacts changes.
- [ ] Existing portfolio process wiring, PRODonlysupport/TESTunchanged, minute-bucketindependence/errorisolation.
- [ ] Raw/effectiveexecutiondiagnosticvisibility/status, no silentunknownfullPnL claim.
- [ ] RelevantSQLite/PG/contract/API/SDKtests and independentfinancial/schema/crossservicereview C0/I0.

## Task F2 — existing small source package
Owner diagnostics_f2_20261027; exact2files reader/test. Benchmark develop/benchmarks/20261027_diagnostics_d1, baseline newlocalplan/f2-baseline.
- [x] Profile/RED largehistory causesbudgetUNKNOWN, minimumSQLnarrowprojectionfix.
- [x] 19targettests/ruff/black; benchmarkbothbeforeafter8cases.
- [x] IndependentreviewC0/I0/M0; statusnotconfusedwithmedian.
- [x] Root19testsPASS (f2-root-tests.log).
- [ ] ActualdeployfreshD1validation (cannotclaimcurrentfixfromlocalprofile).

## Task UI — объяснение полноты финансовых сумм

После финального APIfieldfinancial_status COMPLETE|INCOMPLETE|UNKNOWN маленький leaf-пакет: frontend/src/api/automations.ts, views/PositionsListView.vue/PositionsListView.spec.ts, PositionDetailsView.vue/PositionDetailsView.spec.ts. Только тип и текст для INCOMPLETE/UNKNOWN рядом с существующими PnL/комиссиями, без расчёта денег/разрешений/redesign/deps. Сохранить legacy optionalfield/bootstrap semantics; synthetic UI tests +typecheck/build и independent review. Точный ROOTbrief разрешает leaf gpt-6-luna/low, без субделегирования. Его PRODfrontendimage отдельно аттестовать/обновить; TESTfrontend сохранить.

## Task M — dedicated monitoring
Owner trade_monitor_20261027read-onlyreviewer, rootownsboundedcollectloopandSSH. Snapshotsnewreport/monitor, one every~3min; noautoorders/restarts/resume. Alerts on stale lease/HB, persistentfailfacts/unexpectedHOLD/quantitydrift; normalWAIT/cancelnotstalealone.

## Integration and release
- [ ] Root finalsourcehash/dirtypreservation +appropriateisolatedbackendfinancialrun; migrationroundtrip/fillreplayverified.
- [ ] Independent finalnon-authorreview, image/source/deps/schema/ENVscopeattest and normal-owner deploy plan inclsharedcollector; no existing hardcoded orphan-gen10 helper replay.
- [ ] Rootauthorizedscopedrollout/health/ping/UI+separate fee/tradingread-only observations; stoplocalmonitor oncompletion.
- [ ] Report/CURRENT/WORK_LOG/queue+reviewers+actualruntimebounds. Usercommits later.
