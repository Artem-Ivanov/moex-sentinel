# Торговое восстановление и правило 24 часов — 26.10.2026

## Проверка актуальности перед вехой
Запрос пользователя26Oct явно разрешает командную реализацию, проверку и deploy; записи старше24ч исторические, незавершённые заявки старше24ч — failed. Прежняя веха D1/D3 завершена20Oct, D2 открыта; её runtime не является свежим подтверждением. HEAD f751ab306ba61732727a5057868785882fa79770, большой существующий dirty сохраняется.
Источники: develop/CURRENT.md (20Oct); docs/phase-1-implementation-plan.md (верх/D2); AGENT_BRIEF.md (Core/Worker ownership, admission, outbox); services/order_lifecycle.py, active_intent_gate.py; последний запрос пользователя.

Выполнено ранее: scoped диагностика intent и операций, numeric fix, deployment Core/Worker. Устарело: запрет завершать старые неизвестные intent как failed по возрасту заменён новым решением пользователя. Неизменно: подтверждённые исполнения/позиции/финансовая история/idempotency/outbox сохраняются; нет произвольного сброса всех HOLD или guards. Недостаёт: свежий runtime, точное место cutoff и завершения резерва/системного HOLD, age classification API, regression и reviewed rollout.

## Scope/DoD
1. Свежий read-only API/health срез всех автоматов root; архитектор прослеживает возраст → lifecycle → facts → reserve → HOLD/Resume. Никакого SQL рабочей БД.
2. Оформить минимальные задачи и контракт cutoff >24h (UTC durable created time), неполные факты не считать доказательством неисполнения. Историчность не удаляет lot basis, известные исполнения, дублирование запрещено. Установить, возможно ли штатное автоматическое восстановление только системного HOLD после устранения причины, сохранив пользовательский HOLD.
3. Реализовать согласованные root пакеты с RED/GREEN тестами, independent review C0/I0. Владельцы disjoint files; root один автор планов/контекста. Авторы сначала предоставляют декомпозицию root, после ACK реализация.
4. Релевантные isolated tests (финансовые/межсервисные/граница24ч/повторный запуск/late fill/unknown), миграции только при необходимости с rollback на isolated DB. Без новых branches/stage/commit/push.
5. Root адресно выкатывает проверенные изменения с immutable tags и сохранением ENV/limits/volumes/TEST, controlled worker handoff при необходимости. Не воспроизводить прежние одноразовые ops helpers вслепую. Basic health/UI acceptance, отдельный read-only trading slice после deploy.
6. Подтверждение реального нового решения/заявки/исполнения либо точного остающегося blocker, время и evidence, независимое итоговое review, обновление CURRENT/WORK_LOG/queue.

## Этапы
- [done] Fresh diagnosis + architecture + actual pool/tasks
- [done] Exact package briefs/contracts and root ACK
- [done] Team implementation and independent cross-review
- [done] Isolated verification and rollout preparation
- [done] Authorized deployment, health/UI, fresh trading evidence

## Авторизация/границы
SSH/read-only/адресный deployment и новая policy24h разрешены текущим запросом. Root sole live executor; агенты без live доступа. Не создавать принудительных торговых поручений, не выключать общие лимиты/freshness/risk, не удалять финансовую историю. Возможность автоматического завершения старых неизвестных заявок должна быть явно отделена от доказательства брокерского исхода; существенная недостающая граница уточняется, прочее решается автономно.

## Уточнённые пакеты / root ACK
R1 Worker author recovery24_backend (gpt-6-luna/high): order_lifecycle.py, uncertain_intent_reconciliation.py, storage/repository.py, shared broker_order_policy.py; одноимённые tests, при необходимости storage_dtos/protocol signatures и order_tracking при доказанной watcherветке. Private _age_policy={version:1,expired_at:UTCmsISO}, existing OrderStateChangedPayload FAILED + safe_reason ORDER_AGE_LIMIT_EXCEEDED; terminalNULL означаетbrokerunknown. Все fillmetrics/created/external/idempotency неизменны, expiredrecoveryвыборка отдельнаотdispatch.
R2 Core author recovery24_core(gpt-6-luna/high): services/trading_fact_ports.py,trading_fact_ingress.py,trading_fact_mapping.py,storage/repositories/trading_facts_uow.py,services/trading_admission.py,storage/repositories/trading_admission.py,services/automations.py,storage/repositories/automations.py; associated tests/services/{test_trading_fact_ingress,test_trading_admission,test_automation_lifecycle}.py,tests/storage/test_automation_lifecycle_repository.py,tests/integration/postgresql/{test_trading_admission_ingress,test_trading_admission_flow}.py. Durableorderage/originvalidation, marker noreleaseuntilfreshcompletebrokerdata+Coreledgerequal; atomicallySETTLEDzeroreserve+AGE_POLICY(notNON_SENT)+anchorrenew. KnowntechnicalHOLDonlyconditionalCAS; userHOLDonHOLDpersistsreason/rev. ПроверитьчтоexistingautomaticreconcileработаетдажеприHOLD, включаяalreadyterminalSIBN.
R3 historicalprojection/документация pendingexactbriefafterR1/R2. Independentreviewer recovery24_architect (6.1sol/high) неавтор; итогC0/I0 required. Collectorauthor recovery24_ops(6luna/high), onlynewreport/collect_status.py+test_collect_status.py; independentreviewbeforelive.
R4 exactrolloutsourcepatch/baseattestations pendingfinalR1–R3files; rootsoleliveexecutor. Миграцийнетвproposal. RootisolatedPG поднят tmpfs moex-recovery24-tests-20261026,localhost63850,sentinel_prod_completeness_test, othercontainersunchanged.

## R4 проверка актуальности перед ops декомпозицией
Источники before.json14:06:31UTC/runtime-process.json14:08:46UTC, remote_ops.py:OLD_WORKER_NOT_OWNER иcurrent_hold_states baselineguard. Старый normalrolloutнеподходит: runningWorkerPENDING послеhostrestart, owner10старыйпроцессgone; новыйкодможетштатноизменитьтехническийHOLDпослеlease. Нужен узкий reviewedadapterчерезсуществующиеprimitives: доказатьstopединственногоскопированногоWorkerпроцесса и отсутствиедругих, samehistoricalownercontainerbefore+newStartedAt, freshpendingnextcandidate, unchangedoldowner10/expiredlease→oneCASnew11; неложнообъявлятьcurrentpendingoldowner. ПроверятьсохранностьuserHOLD, разрешитьтолькоожидаемоеproducttechnicalrecovery. Поagepolicyданныенеудалять, rolloutnofinancialmanualoverride. Productreviewpending, точныеpatchhashesпослеfreeze. Opsauthorrecovery24_opsготовиттолькоproposalдоrootACKфайлов, rootsoleliveexecutor.

## Root ACK — R2 protocol completion
Причинаактуализации/источники/DoD в findings.md:R2protocolpreflight, независимыйdesignreviewrecovery24_architect. ЗаменёнdirectCoreresumeнаflaghandshake+узкоесохранениефинансовогосуффиксаотuserHOLDrevisionrace. NewWorkerauthoredfiles automation_lifecycle.py/fact_synchronization.py/synchronize_runtime.py (+tests), Coreauthoringexistingownedfiles+actualdualDBintegration. НикакихновыхHTTP/таблиц/ENV/лимитов; no newuserpermissiongate. Pendingreview/test/deploy.

## Postdeploy evidence preparation preflight
Currentcollect_status.py usespinned20Oct_d1 projectionwhichdropsNEW historical/AGEsettlementflags,anddoesnotfetchrecentdecisionreason. R4DoDneedsfreshalgorithmbranch+actualorders/executions,notcontainerUp. Rootapprovednarrowopsauthorchangeexistingcollect_status.py+test only: retainwhitelistedscalarage/unknown/reasonflags inprojectedD1, foreachscopedautomationboundedGETdecision-reasons?limit5allowlisteditems. Samepins/scope/hard120sec/secretsRAM, nofullpayload; independentreviewbeforelive. Deploymentopsunchanged. Old_frozen_d1helper remainsbytepinned.

## Final source freeze / R4 integration decision
R1/R2/R3 independent architect GO C0/I0, fixtures GO. R2 final no-reserve technical-HOLD issue closed, full manifest9source+6test all SHA matched. Source scope remains accepted24h policy: candidate-source-hashes.json17Core/10Worker, current local relevant sources outside candidate equal current base. No deps/config/DDL. Ops logic GO C0/I0; final data-only allowlist freeze assigned ops author, independent exact map review required. Root backend with isolatedPG in progress; rollout only after PASS. Sources: review-r1-resume-final.md, review-r2-final.md, review-r3.md, core-manifest.json, candidate-source-hashes.json, base-images.json, review-ops-logic.md in current report.

## Rollout gate / current decision
verification.json PASS: full2967PASS+2pg_dumpSKIP+one stale fixture port setup error, only test guard corrected with independent GO and exact full PROD pipeline re-run1PASS. No product source changes after full run; all final cases accounted. Ops+collector root56PASS, Ruff/diffcheckPASS. Source/base⊕patch/sealedmap independentGO; immutable prepare PREPARED (ENVf579...unchanged, owner10/rev517). Root authorizes scoped rollout under explicit user request. Old process currentPENDING container restart23Oct, staleowner10; reviewed orphan stop→freshcandidate→ONE owner-stoppedCAS→11. Health/UI/HBlease renewal first, separate asynchronous financial diagnostic afterward. No forced orders/reserve SQL/userHOLD changes.

## Веха завершена
review-final.md independent recovery24_architect GO C0/I0 по source integration/actual evidence/report/queue. Три свежих исполнения реальны, обе IN_WORK11, outbox0, текущий owner здоров. Ограниченная денежная сверка с учётом комиссий сходится; F1executionlatefee/F2D1budget отдельные задачи, не ложный fullPnL PASS. Report/CURRENT/queue updated, tmpfsPGremoved, sourcehashes/HEAD MATCH. Без stage/commit/push.
