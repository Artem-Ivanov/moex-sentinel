# Аудит торговли и торгового автомата — 02.11.2026

## Результат
Аудит, адресная очистка и локальные архитектурные исправления выполнены с саб-агентами. Старый GOLD BUY139 `55a0cae9-a382-4ba1-a898-c928f7f82f23` завершён штатным AGE_POLICY как FAILED с `terminal_at=NULL`. Core принял ровно одно проверенное событие `25980666-7446-4ae9-8184-7ff806da54e9`, sequence374575, expected_revision15. Отсутствующий BUY reservation не создавался; финансовый candidate и settlement не создавались.

Пользователь отдельно разрешил обслуживание с HOLD и Core-only deployment. GOLD остаётся HOLD revision15, SIBN — HOLD12, `resume_requested=false`. Прежний Worker возвращён в том же контейнере54ce… и образе5e5…, единственный владелец generation13 (`1f7d3387-3e1b-4647-af5e-d5d76e8a462b`); исходная restart policy `unless-stopped` восстановлена. Heartbeat/control progress подтверждены. Финальный адресный срез17:46:39UTC: exact AGE event отсутствует в outbox, pending0/failed0. Между срезами живой Worker продолжает формировать факты, поэтому нулевая очередь не объявляется непрерывным или атомарным состоянием. На приёмке broker runtime RUNNING/NO_DECISION_FRAME; это не означает возобновления стратегии. Resume и новые заявки не выполнялись.

Финансовая история, ключи, persistent volume и отдельный GOLD SELL reserve94 сохранены. Старый другой GOLD SELL18 `80e66106-a1a2-418a-895d-de74a81a754c`, ранее FAILED/AGE_POLICY с неизвестным исходом, защищён от изменений. Физического удаления финансовых строк не было.

## Архитектурные находки и исправления
Core PostgreSQL хранит авторитетные финансовые факты и допуск; Worker SQLite — durable intents и outbox; Analytics — рыночные данные. Эти границы сохранены, новые слои, зависимости и миграции не добавлены.

1. **Important: возрастное завершение зависело от остановленного broker loop.** При permanent broker error runtime ждал close, поэтому заявка не достигала age-policy обслуживания. Исправлено локальное обслуживание под существующим run_lock с прежней паузой recovery_probe_seconds. BLOCKED сохраняется, Broker/Core/Analytics RPC в этом обслуживании не вызываются. Переиспользуется штатный expiry и общий helper healthy/blocked путей; запись факта доставляется обычным control/outbox механизмом.
2. **Important: ошибка до terminal commit запрещала следующую сверку.** Ключ оставался в `_attempted` после исключения или отмены, хотя транзакция откатывалась. Исправлено освобождение ключа в finally; cooldown и durable idempotency сохранены. Проверены failure→retry и cancel→retry без повторного submit.
3. **Important: Core связывал административный AGE с обязательным финансовым reservation.** Полный read-only audit доказал отсутствие exact BUY55a0 reservation во всех состояниях. Core теперь принимает AGE существующего scoped order без reservation после строгих owner, возраста >24ч, неизменяемых order/payload полей и decision-lineage проверок. Эта ветка не создаёт reserve/candidate/settlement; при существующем reservation прежние origin/state/economic guards сохранены.
4. **Minor: диагностика теряла точный RPC и безопасный mapped code.** Добавлены статические RPC labels и allowlist кодов. Raw exception, account, metadata и токены не выводятся. Это не устанавливает причину старого инцидента.

Worker исправления находятся локально в пяти src-файлах. Core исправление выложено отдельно: image7fab5ffd…, модульSHAed88f118…, остальные сервисные образы сохранены. Worker patch не выкладывался — последнее разрешение deployment относилось только к Core.

Предположение об active-only поиске проверено и отозвано: production recovery уже использует exact GetOrderState с REQUEST identity и echo-проверкой. Новые дефекты финансового settlement в проверенной области не доказаны.

## Сверка и границы финансового вывода
Перед продолжением обслуживания свежий snapshot 17:35:52UTC подтвердил 0 ordinary/stop orders и все16 операций с26Oct00:00МСК с полной пагинацией. Перед записью runner повторил fresh preflight. Оба точных старых request lookup оставались UNKNOWN/REQUEST_NOT_FOUND, с BROKER_REQUEST_IDENTITY_MISMATCH и POSITION_MARK_STALE. Отсутствие активной заявки не доказывает отсутствие исполнения. [FAQ T-Invest](https://developer.tbank.ru/invest/services/orders/faq_orders), [request identity](https://developer.tbank.ru/invest/api/orders-service-get-order-state).

Оба рассматриваемых ключа относятся к GOLD. SELL `a6685cd6-9f6f-426b-be0d-c274df743e7f` в Worker уже FILLED/emitted, а его отдельный Core reserve остаётся IN_FLIGHT94 без AGE marker. Этот резерв не освобождался и не переводился в settlement. Единственный unresolved reservation, его полный persisted payload и суммы совпали во всех четырёх Core-срезах обслуживания.

Неизменность подтверждена для8 executions,5 position cycles,5 lots,4 allocations и точной истории80e: order, reservation,3 order-state events,3 связанных accepted envelopes,0 executions. Worker all-column hash80e также совпал. На рабочем Core схема0007; F1 fee-table отсутствует и отмечена как table_absent, а не как пустая финансовая таблица. Полный диагностический audit-log не пересчитывался; он не входит в утверждение о финансовой хеш-сверке. Net PnL и окончательный финансовый исход старых ключей этим аудитом не установлены.

## Обслуживание
Native expiry против живого Worker имеет гонку до первого write, поэтому использован единственный writer. После поддержанных HOLD прежний Worker остановлен штатно с exit0/PID0 и отключённым restart. One-shot на прежнем образе зарегистрирован PENDING, получил поддержанную передачу owner11→12, выполнил только адресный BUY expiry и точный ACK, затем завершился. Тот же обычный Worker запущен с новой runtime identity и получил owner12→13. HOLD, outbox, heartbeat, Core facts, финансовые хеши и restart policy проверены до завершения.

Первый one-shot завершился до PENDING из-за пропущенного обязательного аргумента sleep; owner/AGE writes в той попытке отсутствовали. Wiring исправлен и закрыт main→real-function signature regression и реальным RO main-construction probe на старом образе. Продолжение с сохранённого stopped checkpoint отдельно проверено независимым агентом и read-only dry run. Оба служебных контейнера сохранены для доказательств. После Core promotion также был исправлен root callback contract; окончательный Core deployment имеет receipt PROMOTED_HEALTHY. Рабочая БД для тестов не использовалась.

## Проверки и кросс-ревью
- 191 связанный Worker тест PASS.
- Core unit + HOLD helper:80 PASS. Целевой PostgreSQL16 suite:6 PASS, включая recovery_only/strategy=false/config=None и атомарный отказ при чужом recovery runtime. PostgreSQL отдельный, tmpfs, loopback; production tests не выполнялись.
- Финальный operational пакет:53 fake/unit PASS, включая реальные main-аргументы, failed stop checkpoint, exact ACK, сохранность финансовых данных и lease-граничные случаи.
- Полный backend:2802 PASS/22 FAIL/206 SKIP. Те же22 failed node IDs воспроизведены на исходном HEAD; новых падений нет. Полный набор остаётся RED, эти исходные отказы — отдельная задача.
- Ruff/Black и diff check пройдены в изменённых областях.

Архитекторы: architecture_audit, reconciliation_audit, maintenance_architect. Реализация: runtime_fixes, core_age_fix, maintenance_engineer. Независимые reviewers: independent_review и maintenance_reviewer. App patch, operational helpers и итоговая интеграция проверены неавторами; существенные замечания исправлены и перепроверены. Postfact review actual live receipt: C0/I0/M0. Дополнительный read-only срез17:44:42–17:44:55UTC подтвердил прежние финансовые хеши, Worker FAILED/AGE, has_active_intents=false и оба HOLD. Reviewer проверял исходники и evidence; самостоятельных SSH/live/тестов не выполнял.

Evidence: `develop/reports/trading-audit-20261102/maintenance/` — core-deployment-retry-receipt.json, host-continuation-readonly.json, host-continuation-live.json, post-maintenance-verification.json, final-outbox-ack-proof.json, core-age-strategy-false-pg-root-final.log; предыдущие неуспешные receipts сохранены отдельно. План: `develop/.planning/2026-11-02-trading-architecture-reconciliation-2026/`. Веток, stage, commit и push агенты не выполняли.
