# Реализация допуска PROD торговли

09.10.2026. User explicitly requested design→implementation→PROD deploy.
Goal: полный P3 по [spec](../specs/2026-10-09-production-admission-design.md),
проверенный rollout READ_ONLY/disarmed. P4/P5 реальные действия отдельно после
владельческих UID/финансовых limits. No branches/stage/commit/push. Root ведёт
контекст, интеграцию и ops; writers ниже не меняют чужие файлы.

Актуализация владельца 09.10.2026: проверенная миграция применяется прямо к
рабочей БД; после запуска достаточно health/ping и базового интерфейса,
включая read-only допуск DISARMED. Полные проверки целостности/финансовых
графов и backup/restore rehearsal исключены из текущего rollout DoD;
соответствующие шаги исходного плана ниже исторические и не продолжаются.
Каждая миграция имеет проверенный rollback-скрипт с безопасными условиями.
Текущий регламент: [P3 rollout](../../deployment/production-cutover.md#p3-подготовка-допуска-торговли--09102026).

## Пакеты и зависимости

1. **Shared contract — Core senior prod_admission_backend, 6-luna/high.**
   `src/sentinel_contracts/trading_admission.py` новый и additive
   `src/sentinel_contracts/broker_execution.py` configuration_fingerprint и
   optional PreRpcAbortProof на BrokerOrderState; additive optional proof на
   `src/sentinel_contracts/trading_facts.py` BrokerOrderStateChangedPayload.
   Typed RuntimeRegistrationRequest/result, AdmissionLimits/View/Baseline,
   ReconciliationRequest/result, Arm/Revoke/OwnerStopped, SubmitRequest/
   Permit/Denied(outcome_uncertain), SubmitAttemptView, enums.
   OwnerStopped selects candidate; reconcile UID optional account-only/noarm.
   Submit request deadline + permit server_response_at; bound technical grant1s
   after freshreads, conservative monotonic RTT deduction. OnlyCANARY_UID_GROSS.
   Strict finiteDecimal/positiveint,
   extra forbid/UTCms, secrets headers only. Tests
   `tests/contracts/test_trading_admission.py`. First RED meaningful validation
   and defaultclosed; GREEN; root+independentreviewer contract ACK прежде consumers.

2. **Core storage/admission/auth — тот же Core senior.**
   New api/worker_auth.py, domain/trading_admission.py (при необходимости),
   services/trading_admission.py + trading_admission_ports.py,
   storage/models/trading_admission.py, storage/repositories/trading_admission.py,
   views/trading_admission.py + internal_trading_admission.py,
   adapters/tinvest/admission_risk.py, alembic/versions/0004_trading_admission.py.
   Existing config.py/composition.py/api/app.py/internal_automaton.py/
   internal_trading_facts.py/automaton_brokers.py/automation_commands.py/
   trading_fact_ingress.py/storage/models/__init__.py; exact paths validate
   before ACK, no unsolicited refactor. Protected old internal credentials/facts,
   ownerpin+runtime secret, no expiryhandover, CASoperator actions, baseline+anchor,
   riskdirectbrokerreads then accountrowlock/permanentreserves/no lostACK regrant.
   Configure operator routes through existing auth+CSRF/run_sync. READ_ONLY and
   missingservicecredentials failclosed, no broker writes by Core adapter.
   Tests owned Core tests/api/test_worker_auth.py/test_trading_admission.py,
   tests/services/test_trading_admission.py, tests/integration/postgresql/
   test_trading_admission.py + test_trading_admission_migration.py;
   fact ingress atomically consumes exact authenticated durable pre-RPC abort
   proof (zero fills/order ID) без release generic CANCELLED; owner history
   разрешает replay proof recovery owner после explicitstop transfer.
   update migration head consumers and oldauth fixtures narrowly.

3. **Worker transport/admission/recovery — separate senior, 6-luna/high.**
   After shared contract ACK: adapters/core_client.py,
   adapters/tinvest_broker_session.py, services/trading_admission.py new,
   composition.py/config.py/domain/dtos.py/domain/errors.py/order_dispatch.py/
   decision_materialization.py/streaming_batch_tick.py/
   uncertain_intent_reconciliation.py/runtime/streaming_coordinator.py,
   services/recovery.py, storage/repository.py, domain/ports.py.
   Register processsecret+instance and scopedheaders. Effective admission before
   intent; finalSDKgate everypost/retry + monotonicstart/deadline/fingerprint.
   Knownnosubmit vs UNCERTAIN distinction; preserveSQLite state/idempotency/facts,
   retainreserves/unresolvedkey. Durable pre-RPC abort proof атомарен с
   CANCELLED0 JSONoutbox; acceptedgrant только beforeSDKpost, no proof after
   lostACK/SDKentry, no SQLite schema changes. PRODrequest-idOrderState attribution,
   retained precrash active phases read-onlyrecovery/noresubmit: preserve original
   TRADE capability with admission RECOVERY_ONLY/disarmed; existing
   READ_ONLY+active-intents guards remain. SubmitAttemptView absence proves
   never-granted only retired generation. No operation
   heuristic; TEST old behavior same. OWN existing relatedWorker
   tests and tests/trading_automaton/services/test_trading_admission.py.
   No new SQLite fields without rootACK. No Core/sharedcontract writes.

4. **Deployment/operator surface — separate assigned leaf after corefreeze.**
   src/moex_sentinel/operator_admission.py new boundedCLI, compose.yml,
   deploy/remote/.env.example/.env.production.example/preflight.py,
   relevant compose/preflight/CLI tests. API auth+CSRF, password secureinput,
   no tokenprinting/financial defaults. Protect PROD servicecredentials mapping,
   preserveTEST defaults/resources/networks/volumes. Preflight allows PRODTRADE
   only implementedadmission/auth/config; defaultREAD_ONLY/strategyfalse.
   Root writes docs/deployment/production-cutover.md, queue/AGENT_BRIEF context.

5. **Independent review/integration — prod_admission_reviewer,6.1-sol/xhigh.**
   Review packages and finalintegration independently (notauthor). C/I fixes
   by owner and repeatedreview. Auth/security focus part of explicitlyassigned
   trust-boundary review; fullS1/S2 isolation not claimed. Record actualcommands,
   scopes and tests evidence; reject skippedPG asPGPASS.

6. **Fresh VPS cutover — root ops + bounded reviewedhelper author.**
   Freeze tested source-tree hash (uncommitted permitted; user commits), build
   nativeLinux images using the preserved immutable base and its verified actual dependency inventory, plus exactsource manifest; no
   SOURCE+workingtree drift. Capture currentbaseline/private env/readonlymode.
   Shared migration dependency: согласованная остановка TEST+PROD writers,
   затем непосредственная migration0004 один раз через TESTschemaowner.
   Запустить compatibleTESTCore+PRODCore, TESTcollector, прежний TESTWorker,
   затем PRODWorkerREAD_ONLY/disarmed/scopedauth.
   Existingcaps/networks/UID/volumes preserved, originalPGID, no datarestore.
   Native source/import/version hashes уже приняты. После запуска проверить
   health/ping, HTTPS интерфейс, вход, базовые страницы и read-only DISARMED.
   Новый backup, restore rehearsal, fullgraph/integrity и расширенная TESTdelivery
   приёмка исключены из текущего rollout. Ранее проведённые операции — история.
   Откат по безопасным условиям migration; при заполненной admissionhistory
   только schema0004compatible code rollback с сохранением данных.

7. **Close** — root context/log/queue/runbook, finalindependentruntimereview.
   P3 done после принятых code-contract tests и текущей базовой deploy-проверки.
   P4complete day,
   monitoring acceptance and P5livecanary remain open until ownerlimits+explicitarm.

## Проверки по контрактам (RED→GREEN)

- Invalid/missing UID/caps, nonfinite/float ambiguity, lotsbool, wrongcurrency/
  multiplier/commission, marketTTL/stalesnapshot/cashflowunknown => noRPC.
- Two nativePG concurrentpermits observe samebroker snapshot; accountlock
  ensures totalcash/exposure/outstanding/SELL inventory limits, no BUYSELLnetting.
- WorkerApausedaftergrant expiry/revoke→Bdenied; renewalno resurrection,
  stopattestationCAS/reconcile thenexplicitnewarm. Lease/token/instance replay.
- Revoke beforegrant noSDK; aftergrant pending/UNKNOWN retained; queued/retry/Core
  outage/HTTP lostACK returns no newpermit/key. ALREADY_CONSUMED neverfakecancel.
- Partialfill+cancel/replay/corrections/restart preserveactualfills/fees/lot/cycle,
  reserve releaseonlyattributablefreshsnapshot. Foreignmatchingtrade noPRODfill.
- GrantACK→expiry beforeSDKpost→durable CANCELLED0/proof→Core
  CONFIRMED_NOT_SENT/idempotent consumedkey. Crash after proof beforefactACK→
  explicitstop+recoveryownertransfer→sameoutboxreplay, noRPC. Corruptproof,
  genericCANCELLED/lostgrantACK/postexception neverrelease. Crash inSUBMITTING
  afterbrokeraccept and pollingcancel beforeterminal recover/finalizeonce.
- Daybaseline immutable/rearm/restart, deposit/withdrawal adjustment, complete
  pagination/captureambiguity and newMoscowdayclosesarm.
- Forgedoldinternalfacts/credentials/status/heartbeat, wrongprincipal/scope/
  instanceSecret denied; currentownerreconciliation afterrevoke allowed;
  retiredownerdenied; directCoreadapter+missingSDKgate cannotpost/cancel.
- NativeisolatedPG 0003→0004 preservesgraph, constraints andnonemptydowngrade
  refusal; pairedPG+SQLite lostACK/recoveryjournals; TESTfullregression.

## Проверочное окружение

Baseline `develop/reports/layered-solid-20261006/.venv`: Python3.13.16,
critical packages from current uv.lock, realTBankSDK1.49.3; root full2230PASS.
Before rollout also use a separate clean Python3.12.14 environment matching all
51 actual runtime distributions of the pinned native images, before and after
pytest installation. Existing Docker base installs bounded pyproject ranges;
its actual versions differ from uv.lock. Preserve that immutable base, validate
P3/full backend/nativePG behavior on the actual versions, and retain separate
native Linux import/source/UID/SDK/restore/migration/runtime gates. macOS tests
do not alone prove Linux ABI, event loop or SQLite build compatibility.
Local separate PostgreSQL TESTcontainer moex-sentinel-test-20261006-database-1,
127.0.0.1:55433, existing testCompose/testvolume; credentials readprivately from
`develop/reports/postgresql-compose-20261006/test.env`. Existing fixture creates
unique test_schema anddrops onlyit. Rootrunner will set POSTGRES_TEST_DATABASE_URL
internally withoutprintingpassword. Product deployment tested natively Python3.12
onVPS beforecutover. Failednewvenv TLSsetup preserved, no TLSbypass or depsupdate.
Typical: BROKER_ACCESS_MODE=READ_ONLY <venv>/bin/pytest -q <assignedtests>;
<venv>/bin/ruff check <ownedfiles>; <venv>/bin/black --check <ownedfiles>.

## Принятие design и этапов

Existing writtenproduction requirements and current explicit implementation+
deploy instruction are authorizedbasis; no repeated routineuserpermission.
Root reviews concretearchitecture+selfchecks, independentspec/planreview,
then ACK senior exactboundaries before code. Numeric runtimecaps missing are
required activationinputs, not design placeholders or featuredefaults.
