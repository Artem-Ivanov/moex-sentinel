# Execution diagnostics and operations history Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. No branch/worktree/stage/commit/push; user commits. Per-task reports and review diffs live under develop/.planning/2026-10-20-trading-recovery-implementation/.

**Goal:** Реализовать доказуемую scoped диагностику исполнения и проверяемую историю, затем исправить подтверждённую причину UNCERTAIN.
**Architecture:** Existing Worker heartbeat optional projection, Core-owned persisted graph plus authenticated scoped GET. Scoped operations reads one underlying broker page with lossless continuation; old aggregate endpoint preserved. No new database/table/transport.
**Tech Stack:** Python3.12, FastAPI/Pydantic2, SQLAlchemy, SQLiteWorker/PostgreSQLCore, existing broker adapter.
**Spec:** docs/superpowers/specs/2026-10-20-execution-diagnostics-and-history.md

## Global Constraints
- CurrentcheckoutHEADf751ab3; initialdirtybaseline582files in develop/.planning/2026-10-20-trading-recovery-implementation/baseline/. Preserve all prior changes. Review DELTA against baseline, not HEAD-only.
- Russian communication. No branch/worktree/stage/commit/push; no SSH/deploy/workingDB/runtime/trading mutation. Tests BROKER_ACCESS_MODE=READ_ONLY; explicit TRADE only fake fixtures where required.
- Worker owns SQLite/intent/outbox, Core owns PostgreSQL/acceptedfacts, Analytics market-only. No credentials/rawpayload/strategy_snapshot/free error text in diagnostics.
- Exactly one writer per shared file/contract. D1 senior owns heartbeat contract/wiring; D3 senior owns portfolio files; no overlap. Root owns docs/queue/CURRENT/WORK_LOG/integration.
- D1 bounds100automationrows/5intents each; API orderlimit1..20; UTCmillisecondtimestamps; Worker observationTTL30s/futureskew5s. UNKNOWN/stale/ownerchanged never financialproof. Core accepted envelope != WorkerreceivedACK, speculative seq/emptyoutbox != ACK.
- Keep normal heartbeat/control/renewal on optionaldiag failure. Wire compatibility new/old in both directions by internal/runtime capabilitymarker. Strict auth/runtime/broker/account/instrument bindings.
- NOT_FOUND alone never releases reserve/terminalizes/reposts. D2 cause unknown, do not fabricate fix or claim live recovery.

## Review Focus
1. Wrong-runtime or cross-account rows must not bypass scopedGET via globaldiagnostics; owner takeover invalidates oldsnapshot even withinTTL.
2. Optionalcorrupt/slowprojection must not blockcontrol; malformedunknownfieldsold/new rollouttested.
3. DiagnosticGET must perform unlockedreadonlySELECT only, noadmissionrefresh/brokerRPC; boundedfinancialchildren/events and explicitunknown/truncation.
4. Historypagination must not losepage tail; filtered401<500/cursorprovided must not be fullcoverageproof; datetz/mixedsourceerrors constrained.
5. Unknown brokeroutcomes/lostACK/partialfills require correlation and exactly-once facts; offline regression is not evidence of live rootcause.

### Task 1: Scoped execution diagnostics (D1)

**Owner:** one seniorbackend gpt-6-luna/high; independently reviewed gpt-6.1-sol/high. Before code, return exact subparts/files/acceptance for rootACK; then implement TDD. No subagents without root slot/briefACK.
**Files:** Modify src/sentinel_contracts/worker_diagnostics.py; src/trading_automaton/storage/repository.py,runtime/streaming_coordinator.py,composition.py,adapters/core_client.py; src/moex_sentinel/api/app.py,views/internal_automaton.py,services/runtime_versions.py,composition.py,views/automations.py,storage/repositories/order_facts.py,trading_audit.py,trading_admission.py,services/trading_fact_ports.py. Create src/moex_sentinel/domain/execution_diagnostics.py,usecases/execution_diagnostics.py,views/schemas/execution_diagnostics.py. Existingmatchingtests plus new tests/contracts/test_execution_diagnostics.py,tests/api/test_execution_diagnostics.py,tests/storage/test_execution_diagnostics.py,tests/integration/postgresql/test_execution_diagnostics.py. AskrootACK for otherfiles beforeediting. Keepnewcodeasfocused possible, no unused layer.
**Interfaces:** exactly top-level optionalWorkerDiagnostics.execution_diagnostics as spec; WorkerObservation/CommandSyncRownotexpanded. LocalAutomationRepository.execution_diagnostics(broker_id,*,captured_at), boundedactive+recentterminal/CLOSEDhistory. New GET/api/trading-automations/{automation_id}/execution-diagnostics?limit=20&intent_id=UUID, authenticated/no-store. Core uses existingpersistedorders/envelopes/executions/reservations/unlockedownerSELECT. RuntimeStore rawsnapshot keyedtrustedprincipal runtime/generation/broker; dedicatedscopedobservation includesowner/time checks. Existing globalstatus unchangedfinancialshape.
**Compatibility:** GET/internal/runtime marker execution_diagnostics='v1'; validate_runtimelearnsmarker afterexisting env/access checks; CoreClientheartbeat omitsnewfieldcompletelyunlesssupported, includingNone. NewCore accepts missingfield. Boundedoptionalinvalid→UNKNOWN/INVALID_PROJECTION withoutrejectingnormalcontrol. Authidentitymismatch rejects beforepersist/observe. TESTfinancialsourceuntrusted→UNKNOWN/UNAUTHENTICATED_SOURCE, don't changeTESTauth.
**Budget mitigation:** Add src/trading_automaton/storage/execution_diagnostics.py separateNullPool URI mode=ro/query_only reader; samefile/WAL, no mainpool/finalize_lock; deadline100ms/connectbusy<=50ms/VMprogress1000steps; unknownBUDGET_EXCEEDED/nopartialdata/cleanup/dispose. SpecI1mitigation overrides initialrepositorymethodcallback. Missingfile/memoryunsupportedwithoutcreation; callback exactlyboundedreader, not arbitraryPythonhardtimeout. Add deterministic isolatedwriterlock/mainpooloccupied/expensiveSQLinterrupt/cleanup/coordinatorclose tests beforeGREEN.
**Tests and steps:**
- [x] RED tests for boundedactive+terminal/CLOSED/6thtruncation+sourceUTC/privacy, implementationabsent mustfail forbehavior.
- [x] RED wirecompat/controlsurvivesinvalid+repositoryerror, authruntime+broker/account/instrument/takeover, globalstatusnofinance, stale/future/missingoldfield, unlockedreadonlyscopedCorejoins/limit/truncation/exactacceptedfactid. Then minimalcodeGREEN.
- [x] Run focusedcontract/storage/api/runtimeCoreClient suites READ_ONLY, Black/Ruff onlyassignedfiles. PGjoin/scoping against dedicated local tmpfs PostgreSQL16 (127.0.0.1:55434/sentinel_recovery_test), neverworkingDB. Root providesenvforPG.
- [x] Report RED→GREENcommands/results/limits, sourcefilelist+hashes to task-1-report.md inselectedlocalplan. Reviewer checks diff generatedagainstpreexistingbaseline; C/I fixed/rechecked.

### Task 2: Proven cause and recovery (D2; conditional)

**Depends on:** D1deployment plus realcorrelatedintent evidence. Usercurrentlyauthorizedlocalimplementationonly; noauto-live. No proven defect explainingcurrentNOT_FOUNDfound byarchitecture review. Implement no unsafe alternative lookup/stateoverride.
**Deliverable now:** existingrulepreserving recovery procedure packet: collecttimestampedscopedCore/Workergraph forbothunresolvedintents, same-request and onlyidentity-proven same-brokerorder readonlylookups; classifyterminal/pre-RPCabort/contradictory/insufficient. Includeisolatedregression cases lostresponse/lostACK/partialfill/unknown/replay; nofalseclaim these prove livecause. When evidence identifiesdefect, write RED reproduction, minimalcausefix, GREEN/integration andindependentreview. If no sourceevidence, explicitly retain dependencyinstead of markingfixed.
**Potentialfiles afterproof/rootACK:** src/trading_automaton/services/uncertain_intent_reconciliation.py,order_tracking.py,order_dispatch.py;adapters/tinvest_broker_session.py; correspondingtests. Noedits tofinancialbehavior beforeproof.
- [x] Prepareconcretereviewableevidence/recoverypacket and annotatewhatcan'tbeprovedlocally.
- [ ] Afterseparatelyauthorizedruntimeevidence, implementprovenfix andchecks. Pending dependency.

### Task 3: Scoped operations history (D3)

**Owner:** operations_history_senior gpt-6-luna/high, separatefromD1. Finalsinglepagecontract зафиксирован в Spec/D3 final contract; formalreview/rootACK beforeimplementation.
**Files:** src/moex_sentinel/domain/portfolio.py,services/portfolio.py,usecases/portfolio.py,views/portfolio.py,views/schemas/portfolio.py; newtests/api/test_portfolio_operations.py and designatedservice/usecase/adaptercontracttests. Existingcomposition/app.pyareD1writer: use existingview_recent_operations.execute_scoped forwiringwithoutoverlap, orrootACKsequentialhand-off.
**Interfaces:** GET/api/operations/scoped broker/account/from_at/to_at/limit/cursor; legacyGET/api/operations unchanged. Oneunderlyingpage (limit1..500), returnallselectedfilteredrows/no trimmingtail, explicitnext_cursor/sourcecounts/continuation/completeness. Validateownership/environment/returnedaccount, timezoneaware→UTC, datebounds; noarbitrary366days/UTCzerooffsetbusinessrestriction. Non-nullinputcursor/externalerror/filtershortpage neverfullintervalproof. ExactJSONandstatuserrors в Spec/D3 final contract (input_cursor,next_cursor,sourcecounts,exhaustion,coverage).
- [x] RED sourcepagefiltered/nextcursor, twosuccessiverequests/dedup/order/bounds/crossscope/readonly/HTTPauth+validation; preserveexistingAPI/fixtures.
- [x] MinimalGREEN, focusedportfolio/API tests andBlack/Ruff. Nofinancialsnapshot/recoverychanges.
- [x] task-3-report.md results/diff/hash/limitations; independentscopedreviewandfixes.

## Integration and deployment preparation
Rootruns sufficientcombinedbackendregression (includingisolatedPG), checksbaselinepreservation andquery/runtimecompatibility, updatesdocumentation/envonlyifneeded. IndependentfinalreviewmustcloseallC/I. Noautomaticdeployment, noWorkerresumeclear/repost. UserreceivesconcreteimplementationstatusandD2remainingevidencedependency.

## Локальный результат и дальнейшая зависимость
D1 и D3 реализованы и приняты независимыми reviewer `audit_architect_20261020`/`audit_review_20261020`, C0/I0/M0. D2 runbook также принят, но фактическая причина UNCERTAIN/исправление/восстановление не завершены. Для продолжения нужны отдельно разрешённый deployment D1 и scoped runtime evidence; никакого автоматического снятия HOLD/резерва.

Проверки: общий backend2899PASS/2restoreSKIPбезpg_dump/1setuperrorиз-за строгогоимениБД; тот же PROD pipeline отдельно1PASS в требуемой ephemeral DB. Final minor delta (один completenessflag и HTTP tests) проверена31API/storage/auth/PGtestsPASS. Ruff/Black и whitespacePASS. База только локальная isolated PostgreSQL16/SQLite, без рабочих данных. Полный прогон не выдаётся за повторённый после minor delta. Подробные receipts: develop/.planning/2026-10-20-trading-recovery-implementation/task-1-integration-review.md,task-2-final-review.md,task-3-final-review.md.
