# PROD: ресурсы, размер БД, структура и причина отсутствия торговли

## Цель / свежая сверка root — 10.10.2026 18:56 МСК
Пользователь просит текущие ресурсы приложения, размер БД на диске, диаграмму БД и причину отсутствия торговли открытой позиции PROD. Авторизовано read-only наблюдение VPS/контейнеров, SQL metadata/diagnostic SELECT и read-only API; не deployment, рестарт, миграции, удаление данных или изменение торгового допуска в этой задаче. Прежняя отдельная ARM-авторизация сохраняется с gates, но здесь требуется diagnosis.

Источники: git status текущего dirty checkout (preserve); develop/CURRENT.md; AGENT_BRIEF.md:43–89; docs/phase-1-implementation-plan.md:9–65; docs/deployment/remote-compose.md update/rollback. Выполнено ранее quota rollout и TEST retirement; исторический owner gen3 STOP_REQUIRED на14:55 — гипотеза, не fresh proof. TEST apps retired; sharedPG/collector keep. Core owns sharedPG, Worker owns private SQLite. Недостающее: свежие resource/size/status/proof, актуальная metadata diagram и локализация gate/market/strategy блоков.

## Scope / DoD
- Timestamped host RAM/CPU/disk и running containers RAM/CPU/resource limits; различать usage/limits/cache.
- Logical PostgreSQL DB size, physical PG volume/data/WAL и Worker SQLite plus WAL/SHM; размеры других application volumes/локальных изображений по необходимости. Никаких full backups/DB integrity/reconciliation/test queries.
- Диаграмма Core PostgreSQL и отдельной SQLite Worker с реальными FK/границами; проверить live schema head/metadata без application writes.
- Fresh PROD position/runtime/heartbeat/admission/config + bounded recent Worker diagnostics. Установить причинные блокировки и достаточный next step; не объявлять закрытую биржу software failure.
- Независимое review результата/ERD/units/conclusions; сохранить доказательства, CURRENT/WORK_LOG и конкретный отчёт.

## Фазы
1. Сверка постановки/авторизации — выполнено.
2. Параллельные local metadata/gate анализ и root live read-only наблюдение — выполнено.
3. Причинный вывод и диаграмма, независимое review — выполнено.
4. Context/итоговый отчёт — выполнено; ответ пользователю подготовлен.

## Следующий шаг
root выполняет read-only SSH; quota_core готовит ERD по моделям, quota_worker анализирует gate/status contracts; итог review rollout_ops_review.

## Владение файлами
root owns shared plan/context/live SSH and resources/trading receipts. quota_core owns report db-structure.md/.mmd/model-schema.json only. quota_worker read-only code, reports via mailbox. Все production changes запрещены для этого audit. Product code/Git unchanged.

Root ACK локального diagnostic пакета: quota_worker теперь sole writer report/collect_worker_sqlite_readonly.py (stdin collector внутри existing Worker, sqlite mode=ro + connection query_only, SELECT metadata/allowlisted bounded diagnostics; без SSH и любых writes; root исполняет). Scope остаётся read-only audit, privacy/account masking и безопасный вывод обязательны.

## Свежая актуализация root — новые указания пользователя 10.10.2026
Завершены actual ресурсы/PG metadata/ERD/API: shared PG 8.60GB logical/8.71GB physical; 6 running, 542MiB Docker RAM; GOLD IN_WORK18lots, STOP_REQUIRED/recovery_only/effectivefalse, limits/reconciliation null, sessions CLOSED. ERD 23 business tables/45FK matches actualmetadata. Worker actual collector в работе. Источники report/{resources,disk,api,schema-comparison}.json, db-structure.md; gitstatus dirty сохраняется.
Новые прямые распоряжения: написать скрипт очистки БД >1месяц (приоритет логи); удалить тестовые и восстановительные копии с диска. Это расширяет прежний read-only scope только для подтверждённых нерабочих копий. Ресурсный audit/диагностика торговли продолжаются.
Scope/DoD удаления: свежий список точных volumes/архивов и container references; только подтверждённые restore clones/backup archives, без original TEST/PROD volumes, shared PG/collector, working releases и user files. Root выполняет удаление после независимого review конкретного manifest/guards; exact names, без force/prune, до/после df и проверка live mounts/containers. Запрошено пользователем явно, повторное разрешение не требуется.
Scope/DoD retention: сначала read-only проверить все23Core+10Worker таблиц, timestamps/FK/idempotency/runtime ссылки, существующие helpers; предложить конкретные safe policies. Приоритет old diagnostic logs; возраст не разрешает удалить active/reference/unfinished execution/consumed idempotency. Скрипт default dry-run/explicit apply, tests только isolated DB, independent review. PROD SQL DELETE не запрошен; подготовка скрипта не равна разрешению применения. No backups/fullVACUUM/integrity/restart/ARM. Календарный месяц определить явно. Возможный конфликт all-tables age-only vs permanent idempotency зафиксировать и пояснить, не скрывать.
Владение: root shared context, live SSH, exactcopy cleanup; quota_core read-only retention policies; quota_worker actual Worker interpretation; rollout_ops_review independent operation/result/script review. Retention implementation ещё не поручена.

## Retention implementation — свежая сверка root 10.10 19:26 МСК
User explicitapprove shortdesign: «Да, реализовать с этими исключениями». Boundedstagegate complete, implementationauthorized (не productionSQLDELETE). Actualaudit+copycleanup done, finalcleanup_ops_review C0/I0/M0 (minorCLOSEDfixed). Existing33models/consumers audited byquota_core/quota_worker; schemahead0005/Worker10actual; no existingretentionhelper.
Scope/DoD: one maintenance CLI underdevelop/scripts, fixed Core23/Worker10 inventory, safe4tables prune only: trade_audit_events noncritical; business_audit_events noncritical protectedactive/unfinalizedintent andpendingoutbox; valuation/portfolio oldsnapshots preserve latestperscope (ties retain safe). Calendar month UTC strictolder, defaultdryrun/explicitapply, boundedbatches/errorrollback, noVACUUM/schema/financialstatewrites. Reportscounts and preservedreasonalltables, no rowpayload/URLs/secrets. Parse/store validation and no SQLiteimplicitcreate (existing file); separatestoreinvocation respectingownerDB. Testsisolated PG+SQLite behavior cutoff/bounds/critical/latest/active/outbox/persistentkeys/defaultdryrun. Source/CLI/docs integration+independentreview, scriptnotapplyonPROD.
Root implementationowner delegatedquota_core script+Coretests, quota_worker separateWorkertests (fixtures/schema consumers independent), root docs/context/checksintegration. No newbranches/stage/commit/push. Next root provides exactCLI contract before agentswrite.

## Итог 10.10 — выполнено
Originalaudit+ERD+copycleanup acceptanceDONE; actualcleanupreviewC0/I0/M0. Approved retention implementationDONE: script+23/10tablepolicies+CLI/docs, root17targetPASSnowarnings, fullproject2268PASS/2historicalpg_dumpSKIP/232depwarnings, RuffBlackdiffPASS. Independentcleanup_ops_review C0/I0/M0 currentscriptSHA050ef2e9 afterI1UTC/M1CLI fixed+rechecked; rootfoundsnapshot/URIissuesfixedearlier. NoPRODSQLDELETE/ARM/restart/backup/deploy/Gitactions. Root finaldocdelta/contextreview cleanup_ops_review C0/I0/M0; scriptusage defaultsreport, explicitapply required.

Final bounded status acceptance: cleanup_ops_review C0/I0/M0,17targetPASS/2268projectPASS+2SKIP/232existingwarnings, SOURCEunchanged. Agreed audit/copies/retentionpreparation scopeDONE; noPRODapply/ARM.
