# Аудит торговли, сверка заявок и исправления автомата — 02.11.2026

## Цель и авторизация
Пользователь поручил аудит торговли, сверку и удаление отсутствующих на бирже заявок, архитектурный аудит и реализацию исправлений с саб-агентами. Минимальные исправления доказанных дефектов; очистка активного списка с сохранением финансовой истории, ключей и резервов до доказанного исхода. Новые сделки, отмена существующих биржевых заявок и deploy не запрошены.

## Актуализация до декомпозиции (root)
- Прочитаны AGENTS.md, AGENT_BRIEF.md, CURRENT и очередь docs/phase-1-implementation-plan.md:3–84. Исторический runtime29Oct не считается свежим.
- Проверен текущий src/trading_automaton/runtime/analytics_broker.py: permanent BrokerOperationError останавливает итерации до close; order_lifecycle.py содержит отдельную проверку >24ч. Причина live сбоя неизвестна.
- F1/F2 уже реализованы локально по очереди; повторно не включать без нового дефекта.
- Исходные изменения: docs/phase-1-implementation-plan.md и untracked планы28/29Oct; сохранить. Веток/stage/commit/push нет.
- Core PostgreSQL=факты, Worker SQLite=durable intent/outbox, Analytics=market-only. Историю/volumes сохранить; DB tests изолированные.

## Scope / DoD
1. Аудит автомата/сверки: конкретные сценарии, приоритеты, пути и минимальные исправления.
2. Root проверяет operational путь и политику машины. Fresh сверка: broker open orders, exact identity, полное покрытие исполнений; единичный NOT_FOUND не является proof отсутствия исполнения.
3. Завершение отсутствующих локальных активных заявок штатным механизмом при достаточных proof/полномочиях; иначе точная блокировка и готовая процедура.
4. Исправления с RED/GREEN и связанными регрессиями, без новых слоёв/зависимостей.
5. Независимый cross-review интеграции; все Critical/Important исправлены и перепроверены. Контекст/очередь обновлены.

## Пакеты и владельцы
- root: план, operational preflight/сверка, интеграция, контекст и очередь.
- architecture_audit: architect gpt-6.1-sol/high; read-only broker/control/lifecycle, минимальное решение.
- reconciliation_audit: architect gpt-6.1-sol/high; read-only reconciliation/reservations/history, operational процедура.
- independent_review: reviewer gpt-6.1-sol/high; независимый риск-аудит, затем review решения/diff без соавторства.
- runtime_fixes: senior gpt-6-luna/high, single-owner maintenance/retry cleanup/безопасные RPC labels; root ACK записан ниже.
- order_cleanup_ops: senior gpt-6-luna/high, read-only broker snapshot helper/tests и quiesced cleanup procedure, без live actions.

## Фазы
1. Контекст и актуализация: complete.
2. Параллельный архитектурный аудит и сверка: complete.
3. Worker/Core исправления и независимое review: complete; Worker patch локальный, Core deployed отдельно.
4. Проверки: complete в scope; full suite остаётся RED из-за22доказанныхbaselinefailures.
5. РазрешённоеHOLD/Core-onlydeployment/nativecleanup/supportedowner11→12→13: complete поactualreceipt.
6. Финальнаяread-onlyсверка/контекст/отчёт/postfactcrossreview: complete; finaldocsreview maintenance_reviewer C0/I0/M0.

## Следующий шаг
Оставить GOLD/SIBN на HOLD. Обязательнойработывразрешённойвехенеосталось; финальноеревьюдокументовпринято. Workerpatchrollout, baseline22failures иfinancialsettlement — отдельные задачи; Resume/neworders не разрешены.

## Ошибки
- Initial apply_patch rejected duplicate delete/add targets; записи сделаны прямым обновлением planning файлов, код не затронут.

## Решение root после независимых аудитов (до реализации)
Fresh status02Nov13:23Z (develop/reports/trading-audit-20261102/status-1.json): broker BLOCKED с27Oct, control жив; сегодня BASELINE_REQUIRED, effective_can_submit=false, оба IN_WORK, GOLD284/SIBN1broker/Core. Активный GOLD intent остаётся UNCERTAIN. Значит historical29Oct admissiontrue устарело.
Архитекторы architecture_audit/reconciliation_audit подтвердили недостижимость local aging при permanent block; independent_review подтвердил _attempted leak при ошибке/отмене до terminal commit. PROD exact request lookup уже реализован; первоначальное подозрение active-only отозвано и в исправления не входит. Core financial settlement новых C/I дефектов не показал.
Выбран single-owner maintenance в existing broker runtime run_lock с recovery_probe_seconds паузой; control sweep отвергнут из-за координации второго владельца/cache. Полный reconcile и blind autoretry permanent ошибок исключены. Сохраняются BLOCKED, durable Core reserve, history, late fills и idempotency.
ACK senior runtime_fixes (gpt-6-luna/high): runtime/analytics_broker.py; usecases/broker_iteration.py; services/broker_tick_preparation.py; services/uncertain_intent_reconciliation.py; точечные существующие runtime/service tests. Shared age helper используется и healthy, и blocked путями. Второе исправление finally-discard _attempted + сохранённый cooldown. Опциональный узкий diagnostic пакет разрешён: adapters/tinvest_broker_session.py safe RPC operation labels и allowlisted mapped error.code в BLOCKED log, существующий adapter test.
DoD дополнен: strict>24h, идемпотентный FAILED/NULL terminal и typed fact, ноль broker/Analytics/Core RPC во время blocked maintenance, close/shutdown сериализованы, runtime остаётся BLOCKED; transient finalize/cancel→retry→ровно1execution/outbox, no submit. RED/GREEN + связанные регрессии + independent final review. Без DB schema, новых слоёв/зависимостей и deploy.
Ops owner order_cleanup_ops отдельно проектирует fresh exact evidence и native administrative expiry, до реализации ACK/независимый preflight. Работающее приложение не изменялось.

## Дополнительная проверка исходного baseline
Финальный текущий suite:22failed/2789passed/205skipped. Два новых logger capture теста исправлены; повтор завершён, introduced_failures=[] (baseline-comparison.json). Остальные22nodeids повторены в gitarchiveHEADsrc/tests/alembic baseline:22failed (12.42s). Тем самым их происхождение до этой правки подтверждено; это незавершённая приёмка прежнихF1/fixture/API/migration/SQLbudget, не регрессии текущих5src. В текущую веху их реализацию не расширяем; фиксируем отдельный releasegate, полныйPASSнеутверждаем. В AGENT_BRIEF исправлен устаревший local migrationhead7→8 по текущему Alembic исходнику; runtime-schema8 не заявляется, миграция не применялась.

## Веха6 — разрешённое обслуживание с HOLD (02.11, после ответа пользователя)
Пользователь явно ответил «Разрешаю обслуживание с HOLD»: GOLD и SIBN этого счёта временно HOLD; единственный Worker; административное AGE_POLICY с доставкой в Core; сохранить историю/финансовый резерв; оставить оба HOLD без Resume и новых заявок. Повторное разрешение этих шагов не требуется. Это не разрешение на новые сделки, settlement резерва, миграции или rollout локального патча.

### Root revalidation до делегирования
- CURRENT/status/очередь и текущие hold API, owner-stopped API, native expire_intent_by_age сверены. Предыдущий live срез13:37Z исторический; перед действием нужен fresh статус и точное Core/Worker соответствие обеих GOLD identity.
- Подтверждённый путь HOLD: POST /api/trading-automations/{id}/hold без expected_revision payload; прежняя надпись CAS в ops report — предположение, реальный контракт требует проверить свежий scope/state до и после поддержанного POST. Не добавлять выдуманный CAS.
- Native expiry допускается только при доказанном остановленном единственном Worker writer; live external вызов исключён. Не удалять строки, не очищать outbox/keys/лоты, не освобождать Core reserve. Stop/start старого образа входит в разрешённое обслуживание; deploy нового кода не выполняется.
- При новом runtime ID нужен поддержанный owner-stopped переход с реальными identity/quiescence facts и revision. Старые26Octпараметры/hashes/owner10 не использовать как актуальные.
- Важный dependency: проверить, принимает ли Core AGE_POLICY для BUY без показанного permit/reserve, и не вызывает ли startup/control автоматический settlement. До записи выяснить actual bindings/markers и путь доставки при HOLD; запретить автоматический settlement в выбранном сценарии, не отключая финансовые guards.

### Scope / DoD
Fresh read-only preflight всех2automata, точныхorders/reservations/intents, activebroker ordinary/stop. ПоддержанныеHOLDобоих с подтверждениемWorker/control. Адресная остановкаWorker, quiescence/singlewriter/volume proof. Nativeexpiry толькоподтверждённыхактивныхстарыхintent; typedfact/outboxсохранены. Запуск прежнегообраза с корректнымowner/drainer; обаCore/WorkerHOLD, noResume/noSubmit. Coreorderбольшеactive, AGE_POLICY/terminalNULL/unknownсохранены; durablefinance reserve и amounts/keys/history неизменны. НаблюдениеHB/outboxACK/единственныйowner; health/базовыеAPI. Всеscriptsсначалаisolatedchecks+independentreview, rootaloneSSH/livewrites. Внеусловийостановитьсябезспекулятивныхwrites и сохранитьevidence.

### Владельцы
- root: свежий operational статус, план/ACK, исполнение SSH/API, журнал/контекст.
- maintenance_architect: read-only актуальныйsupportedflow и препятствия/пакеты.
- maintenance_engineer: seniorhelperimplementation после rootACK (opsfolderonly), isolatedtests/no live.
- maintenance_reviewer: independentplan/helperpreflight и postfactreview, readonly/no live.

### Следующий шаг
Свежий status и read-only architectural preflight; затем ACK точного helper scope, tests и независимое review до первого POST.

## Веха7 — необходимое дополнение Core к локальному audit patch
### Root актуализация до декомпозиции
Fresh bounded PG readonly snapshot15:06:23Z (maintenance/core-reserves-exact.json): complete=true, BUY55a0 reservation отсутствует во всём broker scope; единственный unresolved — SELLa668 GOLD IN_FLIGHT94 без age marker. Daily scheduler включён. Six Core entrypoint SHA совпали сHEAD (runtime-preflight.json, независимый reviewer подтвердил). Поэтому текущий Core отвергает корректный native BUY AGE_POLICY; stop/expiry/ownertransfer старого image не выполнять. Разрешённый HOLD идёт отдельным принятым helper. Это дополнительный доказанный Important исходного архитектурного аудита и необходимая совместимость локального Worker maintenance.
### Авторизация / scope
Оригинальное поручение архитектурного аудита и реализации правок разрешает локальный Core patch с тестами; deployment Core ещё НЕ разрешён. Не создавать фиктивный reserve/origin, не ослаблять финансовые checks при existing reserve, не менять financial settlement, модели/схему/payload/endpoint. Preserve current HOLD, history, amounts/reserves/keys.
### Решение / DoD
Развязать administrative order classification и financial candidate bookkeeping. Всегда exact owner/scope/oldorder/age strictly>24h/fromstate/targetFAILEDterminalNULL/immutablepayload+decisionlineage проверки; при existingreserve сохранить все нынешние state/origin/economic checks и candidate atomic update. При exactmissingreserve принять только order event/aggregate/envelope, не создавать reserve/candidate/settlement. Прямые сравненияorder↔payload side/quantity/price/requestedamount/key обязательны (ранее транзитивночерезreserve). Runtime auth уже проверяетсяWorkerAuthenticator+UOWauthorize_fact_runtime; невыдумыватьpermit/originproofприabsence.
### Пакет / владелец
core_age_fix, senior gpt-6-luna/high: src/moex_sentinel/services/trading_fact_ingress.py; tests/services/test_trading_fact_ingress.py; tests/integration/postgresql/test_trading_admission_ingress.py. Точные interfaces/декомпозицияSenior→rootACK до кода. Existing repository/ports не менять без обоснования. Independent maintenance_reviewer проверяет diff и tests; архитектор readonly.
### Приёмка
RED/GREEN nativeWorkerexpiry→outboxserialize→Coremissingreserve acceptedFAILED/terminalNULL; SELLa668reserve/state/fullpayload unchanged; nofinancialcandidate. Подменыeconomicfields/scope/owner/decision rejected, exact24h/future rejected; presentreservepaths unchanged; replayidempotency/latefill. Tests SQLiteisolated + отдельная PostgreSQLпри доступности, никакихtestsнаworkingDB. Обязательный independent cross-review C0/I0, общийbaseline22existing failuresнепереобозначатькакновые. Deploy/миграции/Resume/новыесделкинепроводить.

## Веха8 — разрешённый Core-only deployment и продолжение очистки
Пользователь явно ответил «Разрешаю обновить Core» на образ7fab5ffd (один модуль, no migrations) и продолжение ранее согласованной очистки. Повторное разрешение Core-only deployment/HOLD/native AGE/drain не требуется. Никаких Resume/новых заявок/financial settlement/Worker patch rollout.
Root revalidation: source ed88f118… и образ7fab… построены/проверены;80unit/helperPASS,6PG16PASS в recovery_only с mismatch reject;independentC0/I0/M0. ИсходныйCore683c…/container a5a1538…не менялся; actualimportorigin/app/src proofVERIFIED,dependenciesbyte-parity,Config/layers+1COPY. GOLD15/SIBN12Core+WorkerHOLD,owner11/outbox0,SELL94sameunmarked. Liveпоследниесрезынужноповторитьпереддействием; Core-onlyrecreate сохраняетENV/volumes/network/остальныесервисы.
Перед delegation/кодом: выбрать штатный installed Compose overlay/pin promotion, не обходить .compose-ops-installed и не менятьстарыеimagetags/TEST. Rootread-onlymetadataсейчас; Seniorготовитузкийhelper/tests, независимыйreviewдоupdateMarker/up. Новый deploymentнового кодаостаётся Core-only; cleanupdrainerпоархитектуреoldWorkerimage/volume, одинwriter/noSDK/noAnalytics/noage sweep, требуетсвоегоreviewдоSQLitewrite.
DoD: onlybackendimage683→7fab, moduleoriginSHAed88,healthDBschemaOK; прежниеcontainerIDs/imagesостальныхсервисов/Workerowner11/HOLD/резерв94сохранены. Никакихмиграций. ЗатемnativecleanupтолькоподтверждённогоBUY55a0сCorepatched:singlewriterproof,supportedownertransfer,recovery_only,exacttypedAGEfactACK/CoreFAILEDterminalNULL;HOLD/noResume;SELL94unmarkedunchanged. Финансоваяhistory/keys/volumesсохранены. Critical/Importantcloseindependently.

### Веха8 актуализация после Core deployment / до host bindings
Core7fab успешно promoted healthy (retryreceipt), moduleSHAed88; остальные3container/volume/image неизменны. Fresh16:48:53status подтверждаетHOLD15/12, owner11/HB/outbox0; accountadmissioneffective=true, HOLDbusinessgate независим. Hostdriver seniorв2ignoredфайлах, reviewещёpending; устранитьHBlag/baselinefalseassumption доstop. Root callbackscope: точные boundedreadonly Coreorder/event/envelope+persistedreservehash, SQLitequery_onlyallcolumnsprotectedhistorydigest, свежийbroker0ordinary/stop+completeoperations; no financial writes. Дополнительный senior core_age_fix подготавливает readonly Coreverifier для root binding, безSSH; independent reviewerпроверяетпередlive. DoDостался11→12→13/HOLD/ACK/резерв94/история, Workerpatchrolloutнеразрешён.

### Уточнение verifier после fresh readonly dryruns (до следующей правки)
Actual Core runtime0007, F1fee model/table отсутствуют; остальныеmodelclasspresent. VerifierисключилF1import, явноtable_absent; измененияruntimeнепотребовались. JSONCOUNTвautomation_eventsупиралсяв2s на600kWAIT; exactorderstateevents→factIDPKисправлен. СледующийPG57014вполномTradeAuditEventdiagnosticlogqueryпоorder безиндекса(probe3). Это нефинансоваяhistory и неоперационныйDoDпроверкивсейБД. Убираемширокийdiagnosticauditlogscanизразовогoverifier; сохраняемполныеexact80eorder/reserve/orderstateevents+linkedacceptedfacts/executions, SELL94fullpayload, actualfinancialexecutions/cycles/lots/allocations/fees(ifpresent), exactBUYdecision. ДиагностическийCoreжурналнеизменяетсякодом, неутверждатьегополнуюхеш-сверку. Новыхиндексов/миграций/повышениятаймаута/широкогобэкапане требуется. Reviewerпроверяетсогласованностьограничения/новогоsnapshot; testsrootповторит. Workerнеостанавливался.

### Live checkpoint / revalidation до continuation delegation (17:24UTC)
Frozenrunnerпрошёлfreshgates, отключилrestartиcleanstopWorkerexit0/PID0; failedearlydrainer25c8…exit1доPENDING/noownerPOST/noAGE. ActualsafeTypeErrortrace main770→run_age_drainer, обязательныйsleepargнепередан; memfd/env/image/entrypointconstructionсработали. RootACKновыхузкихпакетов: core_age_fix исправляетmain sleep=time.sleep + actualsignature-smoketest; maintenance_engineer добавляет continuationfromstoppedcheckpoint толькоегоhost2files, retainsfailedcontainer, новыйуникальныйattemptname, freshowner11/HOLD/zerowriters/protectedproofs/oldstoppedidentity, skipduplicate stop; остальнойreviewed12→AGEACK→13flowбезизменений. Rootbindingsделаютfreshread-onlyWorkerpreflightсstoppedvolumeвотдельномoldimage read-onlycontainer(noSDK/noauthregistration), freshCore+brokerproof и исходныйoriginalstoppedcheckpoint. НовыйSHA/independentreview/dryrunONLYдоcontinuationlive. НуженобязательныйвозвратнормальногоединственногоWorker/HOLD/restartpolicy; нельзязавершатьсоstoppedowner. Usermaintenanceauthorizationpersisted; новыхdeployment/Resumeнепланируется.

### Итог17:46UTC
host-continuation-live.json MAINTENANCE_COMPLETE; nativeBUYFAILEDNULLexactACK25980666…/374575; restoredsameWorkerOWNER13/solewriter/HOLD15/12/restartunless-stopped. Postfactmaintenance_reviewerC0/I0/M0; finalCore/SQLiteread-only17:44:42–55financial/protectedhashessame/noactiveintents; addressedqueueproof17:46:39targetACKabsent/all0failed0. Root53opsPASS/PG6PASS, full2802/22old/206. OwnlocaltmpfsPGremoved; persistentvolumesuntouched. Reportdocs/audits/trading-automaton-20261102.md/queueupdated, finalreviewrequested.
