# Архитектура и хранение БД — оценка 11.10.2026

## Fresh scope/DoD root до делегирования
Пользователь просит архитектурную оценку статусов, аудита, индексов и хранения на основании фактической схемы/данных БД и предложение решения. Проект уже имеет Core PostgreSQL (23 business tables/45FK по вчерашнему отчёту), durable Worker SQLite (10tables), typed contracts и месячную очистку с финансовыми/дедуп/unfinished исключениями. Источники: CURRENT, AGENT_BRIEF:35–120, docs/phase-1-implementation-plan.md:318–340, develop/database-retention.md, audit-report/model-schema/db-structure от10.10. Они исторические, не подтверждают runtime11.10. gitstatus содержит сохранённую dirtyработу P3/quota/retention/patch; не менять.
Выполнено: ERD/retention code и Core ETF/FUND rollout. Устарело: вчерашние sizes/stats. Недостающее: freshboundedreadonly PGcatalog/table+index/pgstats estimates, bounded samples/status distribution data; текущий код writes/reads/FSM/contracts, workload и retention соответствие.
Scope: архитектурное заключение и конкретный поэтапный вариант; текущий запрос interpreted proposal/assessment, продуктовый rewrite/migrations/DELETE/indexcreate/deploy/ARM не выполнять. RootonlySSH/readonlyPG+SQLitemetadata stats; stats нецелостность и неpostdeployintegrity. SELECT transactions READ ONLY, statement/locktimeout/limits, no payload/token/account IDs in output; no EXPLAIN ANALYZE/full large-table COUNT/VACUUM/ANALYZE/DDL. Оценки rows/selectivity/nullfrac явно помечать, выбирать payloadkeys/length aggregates беззначений. Статусы классифицировать: domain FSM vs independent flags/derived facts, необъединять orthogonal states blindly.
DoD: фактические sizes/row estimates/schema/indexes/status/null/payload aggregates citeddate+limitations; конкретные code/callers evidence; 2–3 approaches и рекомендуемое минимальное решение с priorities/effect/risks/rollback и separate tasks; diagramexisting+target wherehelpful. Independent nonauthor crossreview proposal/evidence beforeclosure; rootCURRENT/WORK_LOG/queue/doc updated. No branch/stage/commit/push/backups/workingDBtests/financialverification.

## Шаги
1. [done] Свежие метаданные/статистика БД и code context.
2. [done] Независимые исследования statusmodels и audit/index/readwrite contracts.
3. [done] Root synthesis/архитектурное предложение и критерии следующих задач.
4. [done] Crossreview, corrections, context/result.

Итог: архитектурная оценка DONE, independent db_architecture_review C0/I0/M0; M1tempfiles46/M2finalcapturecitations исправлены иперепроверены. D1/D2/D3PROPOSED/D4CONDITIONAL, no implementationclaims.
