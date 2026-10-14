# Удаление TEST и четыре доработки БД — implementation plan

Spec: [архитектурное заключение](../../audits/database-architecture-20261011.md) + явное указание пользователя 11.10 удалить все данные TEST из текущей БД и реализовать четыре пункта заключения с делегированием. Метод выбран: subagent-driven development. Approval реализации уже дан этим сообщением, повторный handoff/approval не требуется.

## Fresh scope / DoD root до декомпозиции

Текущий checkout сохраняет dirty P3/quota/retention/patch. Архитектурное заключение принято независимым db_architecture_review C0/I0/M0; код ещё не менялся. Свежий фактический каталог 11.10 12:36МСК подтверждает head0005, 24tables/45FK/69indexes, два nullable CHECK holes, Worker SQLite десять таблиц безCHECK, двойную audit запись; sourcewriters повторно прочитаны при начале этой вехи. Источники: CURRENT, docs/phase-1-implementation-plan.md:D1–D4, report database-architecture-20261011, storage/database.py/business_audit.py/renew_owner текущего checkout. Исторический снимок не подтверждает runtime во время будущего удаления.

Выполнено: оценка/retention helper/старые релизы. Устарело: statusPROPOSED четырёх принятых scopes; допущение сохранять финансовые TEST строки теперь отменено пользователем только для TEST. Недостающее: testedscopedpurge, freshlivecandidate/writer/capacity report, actualTESTpurge; Core CHECK migration; typed Worker states/transitionpolicy/versionedSQLiterollback; компактный decision audit; periodic renewal audit; isolated indexbenefit и обоснованный run_id index; task+wholeintegration crossreview.

## Global Constraints

- Русский. Не создавать ветки/worktree branches; не stage/commit/push. Работать в текущем checkout и сохранять пользовательские dirty changes. Skill worktree/commit предписания уступают этим прямым правилам пользователя.
- Core владеет PostgreSQL; Worker своей SQLite; сообщения только typed HTTP/outbox. PROD финансовые данные, permanent identities, outbox/unfinished ops, настройки и исходные volumes сохраняются.
- TEST удаляется из текущей shared Core PostgreSQL по environment='TEST', включая его финансовую историю: это отдельное прямое разрешение пользователя, не изменение месячной retention для PROD. Исходный остановленный SQLite TEST volume не удалять в рамках SQL purge. Shared PG/collector в TEST Composeproject — зависимости PROD.
- Root единственный выполняет SSH/actual purge/shareddocs/context. Авторы не запускают live ops. Root перед purge имеет concrete dry-run, safetyreview и isolated tests, свежую проверку writers/capacity/health. Collector можно кратко остановить при необходимости безопасной очистки и вернуть обратно; TEST apps не запускать. TRADE/ARM/deploy приложений не входят в эту веху.
- DB tests/EXPLAIN ANALYZE только отдельный экземпляр. Нет mandatory backup/restore или workingDB integrity/financial graph checks. При purge нужны только guards области удаления и receipts количества удалённых rows; это не postdeploy integrity test.
- Каждая schema migration имеет работающий tested downgrade. Не редактировать применённые0001–0005. Rollback не удаляет PROD финансовые операции; при невозможности отказывает и сохраняет совместимую схему.
- Непрерывные sequence/revision/exact replay/changed-content conflict, prefix ACK, bootstrap/recovery/critical/actual order/fill/LIFO evidence сохраняются. Никакого sampling финансовых facts.
- Один fresh implementer per task, TDD red→green, task spec+quality review, затем независимый final integration review. Авторы не создают своих subagents. Изменения задач не коммитятся; review diff считается от task-start file snapshot, не от HEAD с чужими dirty changes.
- Существующий Python3.12 testenv: develop/reports/prod-admission-20261009/.venv-native-actual/bin/python. Root готовит отдельный PostgreSQL testendpoint; URL/credentials не писать в отчёты. Targeted тесты у автора; общий suite у root после интеграции.

## Task 1: Tested scoped TEST purge helper

Owned: develop/scripts/purge_test_data.py, develop/tests/test_purge_test_data.py, develop/database-test-purge.md. Helpers standalone SQLAlchemy; reuse cleanup patterns где подходит, но monthly eligibility не применять к разрешённому TESTpurge.

Interfaces: default dry-run; explicit --apply; только environment TEST hardcoded; DB URL внутри env, не в CLI/output; безопасные aggregated counts/table names и stableerrorcodes. Batch limits/timeouts, resumable повторный вызов, bounded progress. Не выполнять Docker/SSH из helper.

- [x] Сначала mixedTEST/PROD isolated fixture: защищённые PROD facts/config/runtime/admission, TEST financial chain с audited events/decisions/orders/fills/cycles/lots/allocations, repeatedIDs/scopes; pureTEST и shared portfolio runs/errors. Dry-run не изменяет rows; apply удаляет только TEST, сохраняет exactPROD contents; второй apply no-op. Тест запрета unsupported schema/foreign protected references, строки с environment PRODUCTION/NULL не считаются TEST. Увидеть RED.
- [x] Implement targeted predicates на основе actualcatalog/FKgraph, children→parents; не disable/drop constraints/cascade schema. TEST scopes freeze DISABLED до cleanup; зафиксировать closed writer precondition в инструкции. Global portfolio runs: убрать TEST error entries, удалить только доказанно TEST-only runs после snapshots, сохранить shared/PROD и neutral global anchors. Все таблицы explicit classified, unknown schema failclosed. Chunking не сканирует удалённый prefix заново на миллионных events; сохранить PROD under concurrent writes.
- [x] GREEN isolated SQLite support whereusable + отдельный PostgreSQL16 scenario обязательно; midbatch failure безопасен, повтор заканчивает остаток, scope drift не удаляет PROD. Sanitized errors/output no settings/accountIDs/tokens/rowpayload. Инструкция root dry-run/apply/operational fence; без автоматическогоVACUUM FULL/backups.
- [x] Отчёт с files/tests/commands/results/concerns и safety assumptions; task review spec+quality. Root применяет только после review; receipts в develop/reports/database-improvements-20261011/.

## Task 2: Core nullable invariants migration

Owned: src/moex_sentinel/storage/models/automation_facts.py, trading_admission.py; alembic/versions/0006_strict_state_invariants.py; относящиеся tests/migrations + tests/integration/postgresql + schemahead references. Rootfreshscope13:15МСК дополнение: narrow alembic/env.py SQLite handling требуется для atomicbatchrebuild populatedparentFKgraph: FK OFF вне physicaltxn; 0006 BEGINIMMEDIATE толькоеслиinactive; validateFK доcommit/rollbackthenrestoreFKON finally. Old0001–5 untouched, PGpathunchanged. Изменение SQLite dev/test Core migration safety, не postdeployworkingPGintegritycheck. Author подтвердит populatedfinancial/admissiongraph upgrade/down/up и failure rows+CHECK+revisionunchanged.

Interfaces: revision0006_strict_state_invariants/down_revision0005_order_quota_percent. Bootstrap all-NULL либо вся группаnonnull/positive; OWNER/RETIRED generationnonnullpositive; OWNER lease nonnull; PENDING remainsgenerationNULL/leaseNULL. Не изменять publicRETIREDresponse.

- [x] Behavioral RED на partial bootstrap NULL числах и OWNER/RETIRED generationNULL / OWNER leaseNULL; positive allNULL/PENDING/validOWNER/RETIRED cases; миграция invalidlegacy отказывает без потеристрок. Не зеркалить SQL literal в тесте вместо insert behavior.
- [x] ORM + новая migration strict CHECK; guards на incompatible rows до validation, без autofill/delete. SQLite migration tests по существующему проектному паттерну. Downgrade восстанавливает точные старые constraints, данные не меняет; head expectations обновить только необходимые.
- [x] Targeted schema/model/commandreader/admission tests и isolated PG upgrade→downgrade→upgrade, FAILED guard atomic rollback. Enum domain/wire/SQL parity meaningfulcoverage без registry. GREEN; task report и independent task review.

## Task 3: Worker typed states and narrow order policy

Owned: sentinel_contracts/broker_execution.py (если нужен shared enum) + narrow new policy module; Core trading_fact_mapping callers; Worker domain/storage_dtos.py/storage/models.py/database.py/repository.py и необходимые broker/dispatch/recoverycallers/tests. Не редактировать Task2CoreORM.

Interfaces: отдельные AutomationState/BrokerOrderState/OutboxDeliveryState(PENDING/FAILED), существующие serializedstrings сохраняются. Не объединять independentlyrequired flags/timestamps. SQLite versionedupgrade/rebuild трёх state-bearing tables + explicit testedrollback; create_all не считать миграцией.

Fresh root13:27МСК: текущий Worker не использует PRAGMAuser_version (rg source/tests/quotahelper); выбрать стандартный marker0→1 вместо отдельной versiontable, future/ambiguousversion отказ до create_all. Rollback operatorhelper в develop/ с явной downgrade function; не запускать initializer в downgrade. Monthlycleanup catalogue допускает дополнительные таблицы, его rewrite не требуется. Неподдерживаемые generated/hidden/FK/view/externaltrigger/AUTOINCREMENT shapes можно failclosed до DDL, известные ordinary extensions/indexes/owntriggers сохранять. Основной storage contract и minimumlivevalues не расширять framework.

Fresh root ruling до Task3 dispatch: advisory `develop/reports/database-improvements-20261011/state-preflight.md` прочитан и сверён с current writers/tests. Имя BrokerOrderState уже DTO, shared enum называется BrokerOrderStatus с совместимым FactBrokerOrderStatus. Policy учитывает terminal_at: незавершённый FILLED→UNCERTAIN и broker-confirmed correction незавершённых terminal в ACCEPTED/PARTIALLY_FILLED/другой terminal разрешены; после finalization только exact self replay/stale update no-op. Core UNCERTAIN→same-terminal repair сохраняется без duplicate event. EXPIRED согласовать tracking/reconciliation/finalization. FAILED сохранить: с terminal_at immutable legacy terminal; без terminal_at активный блокирующий intent/резерв, recovery lookup по тому же key без post; подтверждённые O/T разрешены, неизвестность HOLD. SDK unknown status не считать отказом. Данные enum и orthogonal flags не сливать. SQLite BEGIN IMMEDIATE до preflight/DDL, actualDDL rebuild сохраняет дополнительные ordinary columns/indexes/triggers; неподдерживаемые зависимости/shape/futureversion отказ атомарно; downgrade снимает только свои CHECK из актуального DDL, сохраняет добавленные после upgrade поля/indexes. DoD включает real Worker outbox→Core mapping всех recoveryedges и isolated WAL upgrade/reopen/down/up/atomicfailure. Legacy fixtures с DISPATCH_PENDING→CREATED исправить через честный seed, запрещённое ребро не вводить. Advisory не означает implementation review или runtime proof.

- [x] Readallcurrent writers/transitions/recovery fixtures. RED на typed invalidstate/outbox disappearing typo, forbidden nonterminal order regression и сохраняемые direct SUBMITTING→FILLED/UNCERTAIN reconciliation/terminalrepair. Зафиксировать semanticsFAILED без удаления persisted value.
- [x] Минимальная puretransitionpolicy по реально поддержанным edges, единая terminal/recoverable classification; enforce в Core+Worker trust boundaries. Types DTOs/callers, CHECK valid sets в SQLite. В upgrade сохранять columns/indexes/intents/outbox/keys/financialrows; legacyunsupported values дают refusal, не silentloss. Schema version marker + downgrade на отдельной SQLite, неизвестную schema не перезаписывать.
- [x] GREEN state/recovery/dispatch/fact tests; isolated SQLite legacydata/WAL/reopen/upgrade/down/up и noidentityloss. Targeted Core ingress terminal/replay tests. Task report+review.

## Task 4: One decision audit summary

Owned: src/trading_automaton/services/business_audit.py; businessaudit/streamingtick/localoutboxbehavior tests; observability docs task delta файл develop/reports/database-improvements-20261011/decision-audit-doc-delta.md (root интегрирует).

Interfaces: record_decision_process вызывает append_audit_events с одним STRATEGY_DECISION_MADE INFO/noncritical; data тот же safeallowlist including cyclebefore/after. Не убиратьactual intent/order/fill/bootstrap/reconciliationcriticalwriters; everydecision сохраняется.

- [x] RED tests WAIT и trade: ровноодинsummary, reason/cash/reserve/marketage/status/cycleanchors сохранены, secretsunsafe fields исключены. Реальнаяappend создает oneaudit/oneoutbox, subsequentfactsequence contiguous; existingcritical/order stages unaffected.
- [x] Удалитьsyntheticmarker generation, оставить summary contract. Callers/fixtures formerly6stages обновить, без cosmeticenumdeletions historicalstages остаютсявалидны.
- [x] GREEN targetedtests, retry/ACK/restart sequence coverage; report 6→1 толькоэтотgenerator, неclaimwholeDBbytes. Task review.

## Task 5: Periodic successful lease renewal audit

Owned: src/moex_sentinel/storage/repositories/trading_admission.py; соответствующие repository/service/concurrencytests; observability delta в develop/reports/database-improvements-20261011/lease-audit-doc-delta.md.

Interfaces: successfullease update/check каждheartbeatбезизмененияcadence; persistentOWNER_LEASE_RENEWED запись впервыедляgeneration, затем раз в300s. Gate опирается на DBподlockсуществующегoscope, неprocess-localcache; firstgeneration/reset/restart semanticsconsistent. Новыезаписи заполняют knownruntime_instance_id/generation fields, backwardlegacy payload readable. Другиеsecuritytypes/expired denial/revision/CAS semantics unchanged.

- [x] RED deterministicclock: renews at0/3/299/300s, lease+lastseen eachtimeupdated, audit0&300only; newgenerationimmediate; restartusesDB; differentaccountsisolated; expiry/wrongownerfailclosed иeventыsecurityunchanged.
- [x] Minimalquerylastsuccessfulcheckpoint поscope/type сexistingindex; comparegeneration/300sinterval, appendknownfields. Повторные auditdecision markers неissue here. No new userconfig/eventtable/bodyhash.
- [x] GREEN heartbeat/security/admission concurrencytests isolatedPG и SQLitetestpatterns; approllback просто возвращаетappend everyheartbeat. Task report+review.

## Task 6: Measured summary index

Owned: develop/benchmarks/portfolio_run_index.py, src/moex_sentinel/storage/models/trading_analytics.py, alembic/versions/0007_portfolio_snapshot_run_index.py, schemahead/index behavior migrationtests. Depends Task2head0006.

Interfaces: только узкий portfolio_snapshots(run_id) B-tree, имя ix_portfolio_snapshots_run_id, если isolatedcomparison подтверждаетвыигрыш. PK/scopedunique/process/timeindexes не удалять; BRINcomparison запись предложения, не implementationбезcaller/benefit.

- [x] Representative isolatedPG16 workload: production-shaped columns, multiple broker scopes/accounts/currencies/run IDs/20000+snapshot rows/latestties; тот же latest_snapshots caller/filter/order, boundedworkload. Baseline latency/buffers/writecost/indexbytes; неиспользоватьworkingDBURLs. RED migration/index missing and behavioral semantic checks.
- [x] Addindex, повторитьsameworkload: recorded querylatency/buffers benefit, ingestion/WAL/indexcost; выбратьindex толькоприулучшении. Новая0007 после0006 + tested downgrade removingonlynewindex; есливыигрышнет, обоснованный отказ неclaimimplementedoptimization.
- [x] GREEN actualrepositoryresults/scope/order/latestties и isolatedupgrade/down/up. Task report+review.

## Root integration / actual operation

Fresh scope перед actualpurge: writersTESTretired, shareddependencyPG/collectoridentify, dry-run precisecounts/per-table classification, secret-free artifactSHA/review, availabledisk/RAM/health. TESTpurge — прямоеразрешение latestuser, no confirmationrepeat. Прерывание partialbatch удерживаетremainingTESTscopeanchors disabled; rerunreviewedhelper only afterreceipt/currentstate audit. PROD apphealth/ping/basicfunction после, не fullDBcomparison/financialintegrity. Не проводить source rollout без отдельного разрешения deployment.

Финальныйroot suite в Python3.12, isolated PG integration/develop tests, Ruff/Black targetedchangedfiles, gitdiffcheck; nofrontendtestsneededнетUIchanges. Fullnonauthorintegrationreview reports allremainingrisks. Обновить trackeddesign/queue/development docs/CURRENT/WORK_LOG и milestones actualvsimplementation distinctly. Branch/commit/push не выполнять.

## Root continuation ruling — 11.10, before operational index implementation

Freshsource/catalog/residual/log evidence: `purge-failure-classification.json` proves statement_timeout for DELETE trade_executions; originalprocess31000/backend1076320 absent in `purge-progress-1357.json`; dry-run `purge-residual-after-first-pass.json` has auditTEST0/globalTEST0 but events4765628/decisions553058/executions125. `purge-empty-audit-metadata.json` records audit heap1454702592B/n_dead0/n_liveestimated12 after ordinaryautovac; frozenauditFKs allincludeuser_broker_id, currentindexesprocess/time/PK have noleading-scope lookup. RepeatedFKchecks overlargeheap cause timeout despiteTESTauditdeleted. Retryingunchangedbeforefix is obsolete.

Narrownecessarycontinuation: rootowned temporarynonuniqueBtree `ix_test_purge_audit_scope ON trade_audit_events(user_broker_id)` created/droppedCONCURRENTLY, supportedPGcatalog+exactownedindexshape guard, defaultdryrun/explicitapply andsanitizedreceipts. This is scopedmaintenance for alreadyauthorizedTESTpurge, not applicationmigration/deployment or changefourthindexdecision. IsolatedPG tests/indexlookupbenefit beforelive, nonauthoroperationalreview; preservefinancialrows/constraints/allotherindexes. Invalidinterruptedindex refusestocreate until explicitownvalidateddrop; neverIFEXISTSblinddrop. DoD includesdropownindexaftercompletedpurge or knownfailure, nohelperduplicate, refreshedTESTdisabledwriters/PRODhealth, existingreviewedpurgehelperunchangedhash. No FULLVACUUM/constraintdisable/backup/restore/PRODfinancialgraphchecks. Task6sourceauthor remainssoleimplementationdelegate; rootoperationalartifact separate from applicationtaskfiles.

Task6routineidentifier ruling11.10: Alembicversion_num VARCHAR32 requires revision `0007_portfolio_run_index` (24chars); filename0007_portfolio_snapshot_run_index.py unchanged. Longproposedrevision33chars failedisolatedPG andwasfixedbeforeacceptance; source/rollbackidentityunique, no publicAPIbehaviorchange/newauthority. Benchmarkproof task-6-benchmark.json supportsaddindex, fullmigrationtestreport/reviewpending.


## Root integration gate — свежая проверка 11.10

Task1–6 исходники и task-start deltas сверены с текущими файлами; все шесть
independent task reviews Approved, предыдущие Worker I1/I2 закрыты. Зависимости
0005→0006→0007, typed Worker→audit/outbox, strict OWNER→lease checkpoint совместимы
по целевым тестам. Source head0007, livehead0005: application deployment/TRADE/ARM
остаются вне scope. Очередь CODE_DONE/NOT_DEPLOYED, D4 CONDITIONAL; историческая
архитектурная оценка не используется как свежий runtime proof.

Root integration DoD: полный `tests/` и отдельно `develop/tests/` на isolated local
PostgreSQL/SQLite, объединённый Ruff/Black owned Python files, независимое broad
nonauthor source review включая connected callers и миграционные rollback contracts.
Joint collection имеет воспроизведённый duplicate pytest plugin, поэтому suites
разделены без изменения кода тестов. Current source report честно отделяет готовые
изменения от продолжающейся live TEST очистки. Review package root-integration-review.diff
строится относительно task1-before.json только по новым task-owned paths, сохраняя
пользовательский dirty baseline; develop README новый абзац проверяется отдельно.

Live purge отдельный operational gate: только один процесс PID33365/startticks493351823
и backend1082400, reviewed helper SHAfd101ff… unchanged; собственный temporaryindex
createdchangedtrue/present_beforefalse. Завершение требует fresh нулевых TEST
candidates/originalerrors, отсутствия обоих purge backends/process, owned concurrent
drop receipt, сохранённых TEST/PROD fences/PVs и health/ping/basic UI. Никакой
проверки всей рабочей финансовой истории, новой полной копии или source deploy.


## Final integration fix — root scope/DoD 11.10

Broad nonauthor review C0/I1/M1: настоящий TEST legacyFAILED после brokerACCEPTED
исключается из recoverablefilter после10с/restart. Rootперечитал всеcallers/
recoverablefilter/gather/PENDING/inferencebranches; условие подтверждено. Минимальное
решение без новой колонки provenance: TEST observedACCEPTED/PARTIALLY_FILLED тоже
recoverable и broker-only вместе сFAILED; predicate применить кlookup/gather/
PENDINGcontinue/nooperationsinference. TESTUNCERTAIN прежний inferencecontract
сохранить. DoD: настоящаяSQLite FAILED→eachopenstate→terminal черезdelay+restart,
одинстабильныйlookupkey/noPOST/nooperations, финансовыйрезерв/ledger/finalization
правильны, canonicalCore/outboxfacts. При mismatch/unknown не делать inference.

Rootfullsuite6failed/2409passed: пять из sharedbatchruntimefixturePENDING/NEW
невалидны в typedDTO; исправить fixtureнаactualDISPATCH_PENDING/ACCEPTED без
relaxingenum/policy. Rootновоедокументированиесловоlegacy запрещеносуществующим
architectureguard; rootзаменилсмысловойрусскойформулировкой. Task1helper/test
lint38/Black2files cleanup толькоformat/topimports/dictliterals/explicitCLIprint
noqa/expectedguardTRY301noqa, логика/catalog/SQL/security unchanged; actualjob
выполняет ужепереданный reviewedoldSHA. 16isolatedtests+lintнужнынаfinalbytes.
Developwhole suite ещёидёт; новыеconcretefailures входят в sameauthor fix scope
послерoottriage, нерасширятьповедениебезобоснования. ONEfinalfixdispatch; затем
independentfix-onlyrereview и новый meaningful wholecheck послеисправлений.


Finalfixscope addition after DEVELOP_INITIAL_FINISHED: separatedevelopsuite
9failed127passed181.29s. Root actualsource/callers checked: five oldsyntheticruntime
harness failures use targetunused.invalid against strictsandboxendpoint and implicit
READ_ONLY whileexpecting syntheticBUY; four HTTPdelivery failures TimedIngress.publish
wrapper lacks runtime_instance_id/generation kwargs nowalwayspassed byrealUsecase.
Expand sameauthor to develop/benchmarks/runtime.py and pg_server.py ONLY: canonical
sandboxtarget + explicitTESTTRADEfakeconnection/workermode underalwaysoverridden
synthetic SDKfactory; forwardoptionalruntimeidentity/generation unchanged toactual
ingress. No weakeningproductionendpoint/auth/admissioncontracts, no ambientenvmutation,
no actualSDK calls. Existing9named failingtests plusnewnarrowforwarding assertion
where needed verifyconnectedharness. Snapshotextendedbeforeedits.

Samefinalauthor scopeaddition: test_v0_delivery.py localFailPeerOnce.publish mustforwardoptionalruntimeidentity/generation withsameupstreamwrapper; snapshotbeforeedit, nofailure/retrysemanticschange. Newtest_final_integration_harness.py narrow nonNULLforwardingregression permitted.

Sameharnessscope rootruling aftercurrentCoreDelivery/synchronizerread: lostACK fake mustraiseCoreOperationError(retryable=True) chainedfromhttpxReadTimeout, matchingrealCoreClient transporttranslation andexistingFactSyncport catch. Preservealreadycommittedambiguousreceipt/pendingreplay, no productionexceptioncatchrelaxation/no new behavior exceptcorrectfakeboundary. ExistinglostACK/restart assertions verify it.

Samepg_server.py rootrulingafterread: NetworkDelivery records lostACK onlyforCoreOperationError whose explicit__cause__ ishttpx.TransportError, matchingactualCoreClienttranslation; rethrowalltypederrors andpreserveimmutablemetrics/replay. HTTP503 hasHTTPStatusErrorcause andmustnotcounttransport/lostACK. NarrowactualCoreClient+MockTransport regression coversboth, productioncatchunchanged.

## Fresh root scope — slow final event batches, 11.10 15:49 МСК
Последний прогресс4.608M; PG n_tup_del4630000→4644000 за213с, residual≈121628events. Тот жеPID34067/start493476510/backend1084158, activeIO/noerrors. Source frozenhelper select/delete keyset+TESTpredicate reread; wholeDoD/PRODPreserve unchanged. Узкий read-only diagnostic actualactive statement class/age+planner statistics and nonexecuting EXPLAIN fixed known SELECT permitted; noEXPLAINANALYZE, nofinancialrows/payload/IDsoutput. RootownsSSH; independentread-onlyadvisor checks source/metadatainterpretation. Не менятьtimeout/batch/source/DDL и неspawnsecondpurge безконкретнойнеобходимости/review.

## Fresh root continuation — tiny parent statistics maintenance, 11.10 15:56 МСК
Freshlivepurge-tail-plan-parent-live.json: exactsinglebackend1084158 SELECTkeys7–28s/DataFileRead; parent3scopes(TEST1/PROD2), reltuples=-1/pages0/noanalyzeever; eventsstatsalreadyfresh. Nonauthorpurge_tail_performance isolatedUUIDschema reproduceswrongSort+BitmapPK→orderedPK/NestedLoop after ONLY ANALYZEuser_brokers(id,environment), parentfingerprint/eventcountunchanged/schemafinallyremoved (purge-tail-local-repro.json). Necessaryminimalmaintenance: fixed public.user_brokers/id,environment only, statement/lock5s, exact11columns/head0005/ownrole/≤10scopes/oneDISABLEDTEST guard; only after helperindependentreview. NoSQLdata/DDL/globalsettings/indexchange, noeventANALYZE/noduplicatepurge. After actualreceipt verifyfreshnumericparentstats/plainEXPLAIN and exactsinglejobcadence; existingzero/fences/drop/basichealth/opsreviewDoD unchanged. PG16officialdocs/sql-analyze.html confirms namedtable/columns stats and parallelordinaryactivity readlock.

## Fresh root scope — empty event heap FK timeout, 11.10 16:12 МСК
Thirdsinglejobexit2; exactPID/backendabsent inpurge-after-third-failure.json. Freshdryrunpurge-residual-after-third.json TESTevents/financialchildren0, onlyautomations6/instruments4332/sync1/broker1. ExactPG1084158 logsclassify statement_timeoutDELETEtrading_automations, FKcontextONLYautomation_events; noFKviolation/deadlock/locktimeout. Automationdependentcatalogpurge-automation-fk-costs.json: eventsheap3890176000B/n_live0/n_dead0, validleading-scopeindex; otherheaps0/8192 exceptownindexedPRODaudit1.454GB. Emptyeventheap remainsallocated across13:03→13:07 afterautovacuum; blindretryobsolete. Leastnecessarymaintenancecandidate ordinary VACUUM(TRUNCATEtrue) ONLYpublic.automation_events, noFULL/rewrite/schema/indexchange/globalsettings/servicepause. StandardMVCCpreservesallliveconcurrentPRODrecords. Rootfixedhelperexact10columns/head5/owner/timeout300s+lock5s/defaultfixedoperation, independentmockreview + isolatedPGpopulatedPROD/Testfixture preservingPROD/otherindex/tablefingerprint beforelive. Afteractualreceipt/catalogheapproof/noactivehelper useunchangedfrozenpurge once batch2000; finalDoDzero/ownedindexdrop/fences/health/basicopsreview unchanged. No additionalbackup/restore/financialintegrity checks.

## Fresh root maintenance correction — 11.10 16:22 МСК
FirstordinaryVACUUMrefusedexit1; allfixedschema/owner/headguards pass inread-onlydiagnostic, noactivepurge. BoundedPGlog exactwindow shows sharedmemoryresize/NoSpace1, noSQLtransaction/lock/statement/FK errors; ordinaryheap unchanged3.89GB. Requiredminimumfixperoperation VACUUM(PARALLEL0,TRUNCATEtrue) exactsameone table/guards/timeouts, avoidingparallelsharedmemoryallocation; noglobalsettings/containerrestart/shmresize. Independentmockreview andisolatedPGordinaryvacuum withPARALLEL0 preservingPROD/allindex/tableproof required beforefixedhelpersecondattempt. ExistingcompletionDoD unchanged; nofreshappsourceedit orduplicatepurge.

## Fresh root final continuation gate — 11.10 16:34 МСК
SerialordinaryVACUUMexit0/lastvacuumconfirmed; heapunchanged does NOT mean tableempty: narrowindexed ENVcount PROD18/TEST0, aggregate lastctid474874 equals relpages474875-1, protectedPRODtailpreventsphysicaltruncate. Prior n_live0 wasestimateonly. No extraVACUUM/FULL/DISABLE_PAGE_SKIPPING/neweventindex/sessionplannerchanges. Freshvalidexistinguq(scope,automation,sequence) reltuples0 afterVACUUM; all6actualTESTautomation FK readonlylookup+plainEXPLAIN returnfalse in1.48–2.19ms combined and IndexScan total6.27. Nooldpurge/backend/vacuum; independentpurge_tail_performance approvesONESAMEfrozenfourthapply onchangedlookupcostcondition, notphysicalshrink. Freshfences/PVs resourcecollector inwork; afterproof singleapply batch2000 thenunchanged finalzero/ownindexdrop/health/basicopsreviewDoD. HistoricalinternalFKplan beforetimeout unknown; noclaimcausalbeforeplan.

## Fresh root basic probe contract correction — 11.10 16:41 МСК
Actualfourthreceipt exit0/4340TESTrows; freshzero24tables/originalerrors0/processbackendabsence and ownindexdrop+absence PASS. Health/ping200ok. Protectedbasicprobe exit1constant; currentactualroutes/views+frontendcaller reread: /trading-automations/{id} returns flatAutomationSchema, /{id}/details returns automation/operations/candles/errors. Probe calledflatroutewhileassertingdetails; leastfix ONLY append/details inreadonlyprobe (notappsource), retainpin/GETbounds/loginlogoutfinally/sanitizederrors. Independentdelta review beforeonelive retry; source65SHAunchanged, fullsuitesnotrepeat. FinalopsI1pendingbasic/completeproofreview.

## Fresh root final documentation/archive gate — 11.10 16:44 МСК
All sourcechecks/reviews completed, frozen65 unchanged; actual fourthexit0/all24TESTzero/originalerrors0/all4processbackendabsence/ownindexdrop+absent/freshfencesPVshead5/healthpings/protectedbasicdetailsPASS. Finaloperationalnonauthorreview writesclosureI1. Requiredfinalscope onlyaccuratedocs/queue/CURRENT/planning/worklog +localSDDarchive39files/43.65MB verifiedperfileSHA/noSymlinks; removeONLYexactownplanworkspace afterarchiveproof andopsApproved. OtherSDDplans/Git/userdirtycheckout preserved. Independent integrationreviewer reads finalreport/docs/context/archivemanifest vsdirectproof, no repeated source suites/network/actions. DoD no falseTOTALdeletion/browserrender/trading/deploy/PRODthroughput claims; preserveall4Rulingsorder+cost. Userownscommit; release is a separatetask.

## Финальная приёмка — 11.10 16:47 МСК
SixsourceTasks complete;2578PASS/65Pythonlint+sourceSHAunchanged; finalsource database_integration_reviewApprovedC0I0M0. Actualfourthpurgeexit0/4340remainingrows, fresh24TESTzero/originalerrors0/all4jobabsence, owntempindexdropped+absent, TEST4fenced/PROD+sharedrunning/original4PVs/imagesflagsmountsrestartsunchanged/head5, health/pings/pinnedbasicdetailsPASS. Finalnonauthorpurge_final_ops_reviewApprovedC0I0M0/I1CLOSED. OwnSDDlocalarchive+manifest verified beforecleanup, finaldocs independentreview follows. No deployment/trade/Gitmutations; sourcehead7/livehead5; exactfullfirstpassreceiptlost/PRODtailpreserved/HTMLAPIonly.
