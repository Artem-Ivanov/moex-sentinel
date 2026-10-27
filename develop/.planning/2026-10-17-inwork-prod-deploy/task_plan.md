# PROD completeness и rollout — 18.10.2026

## Цель и авторизация
Пользователь поручил исправить реальные пробелы PROD, сохранив настоящий TEST/Sandbox и doubles в tests, затем выполнить deployment. Полный scope и TTL-политика подтверждены; 18.10 дано явное «Приступай к реализации после к деплою». Повторное согласование уже разрешённых шагов не требуется. Root координирует, исполнители пишут код, независимые агенты проверяют. Git branches/worktrees/stage/commit/push не выполняются.

Источники: `docs/superpowers/specs/2026-10-17-prod-completeness-design.md`, `docs/superpowers/plans/2026-10-18-prod-completeness.md`, актуальная очередь `docs/phase-1-implementation-plan.md`, инварианты `AGENT_BRIEF.md`. История решений и старых gates сохранена в progress.md/WORK_LOG; прежние receipts не подтверждают текущий runtime. HEAD f751ab3; продуктовые изменения этой вехи остаются dirty.

## Сверка актуальности и решения
- PC1 isolation/unary availability и PC3 diagnostics/alerts/UI приняты после independent C0/I0/M0. PC2 после исправления durable UNCERTAIN, status allowlist, bounded RPC, TTL confirmation window и actual SDK-entry expiry guard получил final independent C0/I0/M0. Evidence PC1/PC2/PC3 в `develop/reports/prod-completeness-20261018/pc*/`.
- PC4 новый полный AnalyticsHTTP→Core market/risk/admission→Worker PROD strategy→SDK-only fake→durable ledger/outbox→authenticated CoreHTTP/PG исполнение прошёл независимо вместе с ingress:4PASS, combined-boundary review C0/I0/M0. Existing PG flow fixture несовместима с active-state guard уже в HEAD: local IN_WORK DTO не публикуется в Core, где остаётся IN_QUEUE; узкий test-only fix поручен PC1 автору до full suite.
- Python3.12 .venv готов; отдельный PostgreSQL16 tmpfs localhost55434/database sentinel_prod_completeness_test. Не использовать PROD SQL/SQLite или только новый schema в неизвестной рабочей DB. Root frontend259PASS/typecheck/buildPASS; fullPython/PG ещё не принят.
- Новый PC5 helper и 21offline tests сданы автором, independent review выполняется; root обнаружил потенциальные реальные wiring defects, helper нельзя исполнять до исправлений/review. Никакие live сервисы ещё не переключались.
- Clean-stop receipt разрешён вместо прежнего helper-specific exact-SQLite-intent freshness gate: independent OPS architect и hashes четырёх oldWorker lifecycle modules подтверждают корректный источник доказательств. Требуется pinned ID/image/restarts/restart=no, exit0/noOOM/FinishedAt после SIGTERM, сохранение полной storage identity. Marker APPLICATION_STOPPED вспомогательный, сам по себе не доказывает drain. Никакого свежего exact-intent/terminal-resolution утверждения без источника.
- Релиз включает ЧЕТЫРЕ immutable image: Core, Worker, Analytics, UI. Core/UI/Analytics совместимые readers идут до Worker. Candidate Compose выводится из active parent с ровно двумя добавленными binding: Core MARKET_TRADING_STATUS_TTL_SECONDS=30 и Worker BROKER_REQUEST_TIMEOUT_SECONDS=10. Private ENV, прочие defaults/Compose drift, caps, ARM/Resume/HOLD не меняются.

## Выполнение и DoD
1. [done] Audit/spec/plan, independent review, pinnedSDK1.49.3, dedicatedtestPG.
2. [done] PC1/PC2/PC3 fixes, financialACCOUNTanchor/closedUIDcorrectivefix, independentfinalC0/I0/M0.
3. [done] FullPRODcomposedPGtest +market/concurrencyfixtures; root2804PythonPASS2pg_dumpSKIP/259UI/type/build/Ruff/Black/diffPASS.
4. [done] ActualRemoteOps/CLI/API-shaped tests, helpers38offlinePASS/18APIchecks, allfoundImportantclosedindependently.
5. [done] Exact350source/34buildinputs/4templates package437, fourcandidateID/source/import/UInginx checks, exactreview0.
6. [done] Rootauthorizedrollout09:02:08UTC: pinnedoldWorkerexit0/restarts0/restartno, readersfirst, samestorage/newWorker, uniquePENDING, oneownertransfergen6, boundedheartbeat/health/UI checksPASS.
7. [done] Publichealth/renderedlogin+validacceptancereceipt, independentprod_ops_reviewC0/I0/M0, runbook/queue/context updated. No actualtradingacceptance/currentintentinspection; limitsnullattransfer. OptionalextraAPIbundle unavailableSSHtransport, cancelledread-onlyrequest; noextraDBinspection.

## Ограничения и stop conditions
- Последние исторические данные17.10: PROD TRADE, admission DISARMED/limits=null/effective_submit=false, IN_WORK+HOLD; одна UNCERTAIN без brokerID/fills/preRPCabortproof. Статус исторический NOT_RECHECKED, ref022634125f04. Не очищать/repost/Resume и не утверждать её resolution. После clean restart durable recovery state должен сохраниться; проверка rawDB запрещена.
- Любой неclean stop (timeout/SIGKILL137/OOM/restart/container drift) останавливает дальнейшие Core/link/owner mutations. Preserve volumes/history. Новая candidate identity/storage/config не должны менять scope.
- Admission invariants/current owner/generation/caps/ARM/config проверяются поддерживаемыми API. Volatile heartbeat/lease/readiness нельзя ошибочно считать immutable финансовым состоянием. Нулевые/отсутствующие лимиты не подбирать автоматически.
- Ordinary deployment не требует полного backup/restore/integrity comparison. DDL этой вехой не добавлено. Rollback только доказанно совместимый application set с сохранением схемы/данных; иначе остановка/forward fix, без старого UID-gated fallback.
- Все Critical/Important устраняются и независимо повторно проверяются до исполнения зависимого шага. Авторы не принимают свои изменения. Root сохраняет итоговые evidence и точную границу проверенного.
