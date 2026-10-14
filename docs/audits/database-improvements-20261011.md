# Улучшения хранения и удаление TEST — 11.10.2026

Реализация четырёх согласованных направлений прошла независимое ревью каждой
задачи. Финальное интеграционное ревью выявило край TEST recovery после FAILED → ACCEPTED.
Он исправлен и независимо перепроверен: Source Approved, C0/I0/M0. Устаревшие
тестовые fixtures и benchmark wrappers исправлены. Полные наборы проверок на
финальных исходниках: 2437 проектных и 141 служебный тест прошли. Удаление TEST из текущей общей Core PostgreSQL завершено: свежий dry-run
показывает нулевой остаток во всех 24 таблицах. Собственный временный индекс снят.
Независимое финальное operational review — Approved C0/I0/M0; I1 закрыт. Deployment приложения, ARM и торговые операции
в этой задаче не выполняются. Рабочая схема остаётся `0005_order_quota_percent`;
source head — `0007_portfolio_run_index`.

Основание — [архитектурная оценка](database-architecture-20261011.md) и прямое
разрешение пользователя удалить всю TEST-историю и реализовать четыре направления.
[План и критерии приёмки](../superpowers/plans/2026-10-11-database-improvements-and-test-purge.md).
Разработка выполнена шестью последовательными задачами с отдельными авторами и
независимыми рецензентами; пользовательские изменения checkout сохранены.
Ветки, stage, commit и push не выполнялись.

## Реализация

| Направление | Результат |
|---|---|
| Состояния Core и Worker | Core CHECK теперь отвергают неполный bootstrap и OWNER без положительного generation/lease. Worker использует отдельные enum и общую узкую политику переходов брокерской заявки; SQLite revision 1 добавляет CHECK атомарно. |
| Decision audit | Одно `STRATEGY_DECISION_MADE` вместо шести синтетических markers, включая WAIT; прежний разрешённый контекст сохраняется. |
| Lease audit | Первое успешное продление поколения и затем checkpoint через минимум 300с; каждое продление по-прежнему проверяет владельца и срок и обновляет lease. |
| Индекс portfolio | Только неуникальный Btree `portfolio_snapshots(run_id)`, оправданный изолированным измерением настоящего repository. |

Незавершённый legacy FAILED удерживает денежный резерв и восстанавливается по
прежнему idempotency key без повторной отправки заявки. Частично исполненный
EXPIRED проходит проверки цены, позиции и уже применённого ledger. Финализированные
операции сохраняют прежнюю неизменность. Enum состояния доставки outbox не смешан
со статусом брокерской заявки. CHECK SQLite используют бинарное сравнение даже
при унаследованной NOCASE/RTRIM collation. Неизвестный/неоднозначный DDL вызывает
отказ до изменения файла.

Decision summary сохраняет безопасный allowlist и непрерывность sequence; retry,
перезапуск, prefix ACK и exact replay проверены через настоящий SQLite outbox.
Реальные financial/order/fill, critical, bootstrap и recovery writers остаются.
Старая история не удаляется. Lease checkpoint хранится в существующем audit и
читается под текущими блокировками account → runtime → admission; конкурентный
renewal и restart не сбрасывают интервал. ARM, STOP, owner transfer, expiry и denial
не сокращены. Новые successful events заполняют структурированную generation;
старые NULL читаются из payload, структурированное значение имеет приоритет.

## Миграции и откат

`0006_strict_state_invariants` проверяет допустимость строк под блокировкой до DDL;
неполные данные требуют отдельного явного исправления. Downgrade возвращает
прежние CHECK и сохраняет все строки и новые операции. `0007_portfolio_run_index`
добавляет один индекс, downgrade удаляет только его; новые снимки сохраняются.
Обе миграции проверены upgrade/downgrade/re-upgrade на отдельной PostgreSQL и SQLite,
включая populated financial graph и все прочие индексы.

Worker revision `PRAGMA user_version=1` проверена отдельно на SQLite, включая
колонки, implicit rowid, собственные индексы/триггеры, WAL и новые данные после
upgrade. [Rollback script и условия](../../develop/worker-state-rollback.md) снимают
только собственные CHECK в одной транзакции для остановленного Worker. Неизвестные
версии, зависимости или изменённые CHECK вызывают отказ. Откат audit-генераторов
происходит возвратом совместимого приложения без изменения данных/схемы.
Ни один rollback не выполнялся на рабочей БД.

Обычный CREATE INDEX в 0007 может задерживать записи на время построения;
применение миграций относится к отдельному deployment. Приёмка deployment — health,
ping и базовый интерфейс по принятой политике, без обязательной полной копии и
проверки всего финансового графа рабочей БД.

## Измерение индекса

Отдельный PostgreSQL 16.15: 43 200 полноформатных снимков, 600 runs, восемь
TEST/PROD scopes, три счёта/валюты, одинаковые времена последних двух runs.
Четыре фазы без/с/без/с индексом сохраняют полные DTO и порядок настоящего
`latest_snapshots`. Это синтетический workload с тёплым cache.

| Сценарий | Median, ms без → с | Shared buffers без → с |
|---|---:|---:|
| Latest, все scopes | 8,783 → 4,726 | 1080 → 4 |
| Latest, TEST | 8,048 → 4,925 | 1081 → 5 |
| Latest, PROD | 8,395 → 3,858 | 1081 → 5 |

Индекс занимает 336 KiB. Для 3600 новых снимков и 50 runs зафиксирован WAL
2 933 464 → 3 292 856 B (+12,25%). Среднее время двух committed ingestion samples
выросло на 10,24%, с заметным шумом. Это цена конкретного измерения, не доказательство
production throughput. Другие индексы и запросы не изменены; BRIN и разделение
permanent receipt/diagnostic body остаются отдельными условными задачами.

## Удаление TEST

[Разовый helper и правила](../../develop/database-test-purge.md) разрешают удаление
всей TEST-истории только по точному владельцу `environment='TEST'`. PROD-данные,
постоянные ключи, незавершённые PROD-операции и четыре исходных Docker volumes
сохраняются. Остановленная оригинальная Worker SQLite — отдельное хранилище;
удаление из текущей общей Core БД не означает удаления её исходного тома.

Первый dry-run обнаружил 9 010 653 строки и десять TEST portfolio error entries.
Первый apply удалил TEST audit, portfolio snapshots/runs/errors, allocations и
лоты, затем остановился по statement timeout при проверке FK `trade_executions`.
SSH-соединение завершилось раньше процесса, поэтому точный финальный receipt
первого запуска потерян; PostgreSQL общие счётчики не считаются TEST-only receipt.
Его процесс и backend завершились до запуска продолжения.

Причина подтверждена метаданными и безопасной классификацией PostgreSQL log:
оставшийся audit heap 1,454 GB без ведущего индекса scope сканировался при каждой
FK-проверке. На отдельной PostgreSQL временный индекс
`trade_audit_events(user_broker_id)` сократил lookup с 207 до двух страниц.
Helper concurrent create/drop проверен тремя тестами и независимым ревью.
Фактический CREATE завершился с `present_before=false, changed=true`; индекс
принадлежал этой операции; после завершения DROP CONCURRENTLY завершился
с present_before=true/changed=true, затем dry-run подтвердил present_before=false. Не отключались
FK, не выполнялись VACUUM FULL или удаление PROD-истории.

TEST Core/Worker/frontend/Analytics остаются остановленными с restart=no.
Общий collector возвращён после portfolio phase: текущие repository/service
контейнера заново читают scopes каждый цикл и пропускают DISABLED. TEST-приложения
не возвращались. Второй apply удалил 553 058 TEST решений, 125 исполнений, 131 заявку и 392
события заявок, затем достиг statement timeout на position_cycles. Fresh metadata
после завершения процесса показывает пустой heap решений; autovacuum завершён.
Fresh catalog подтвердил, что все шесть зависимых heaps пусты либо занимают
одну страницу; третий проход удалил 34 TEST цикла и все TEST события, затем достиг
statement timeout на trading_automations; точный FK context — automation_events.
Все прежние процессы и backends отсутствуют. Fresh dry-run оставил только
4332 instruments, одну sync state, шесть automations и один TEST broker.
Пакет продолжения — 2000 строк.

Последние SELECT batches ускорены узким ANALYZE user_brokers(id, environment):
до этого статистика tiny parent никогда не собиралась и план сортировал event
кандидаты. Изолированная репродукция и actual plain EXPLAIN подтвердили переход
к ordered PK/Nested Loop; данные и DDL не менялись.
[PostgreSQL 16 ANALYZE](https://www.postgresql.org/docs/16/sql-analyze.html).

Heap automation_events после удаления TEST остался 3 890 176 000 B;
оценка n_live=0 не означает физически пустую таблицу. Обычный targeted VACUUM
проверен отдельно: PROD строки, все индексы и другие таблицы сохранились,
пустой fixture сократился до нуля. Первый actual VACUUM отказал из-за
shared memory resize/NoSpace. Вариант с PARALLEL 0 прошёл повторные независимые mock/isolated проверки
и фактически завершился exit0. Heap сохранился: узкий indexed count обнаружил
18 живых PROD events, последняя строка находится на последней heap странице
474874. Эти строки сохранены. Повторный VACUUM, FULL, отключение page skipping
или ещё один event index не потребовались. PARALLEL 0 отключает parallel vacuum
только для данной команды.
[PostgreSQL 16 VACUUM](https://www.postgresql.org/docs/16/sql-vacuum.html).

После VACUUM все существующие event indexes валидны, reltuples обновлена.
Все шесть актуальных TEST automation lookups возвращают false за 1,48–2,19 мс
вместе с plain EXPLAIN; custom FK plan использует existing composite index.
Prepared generic plan тоже indexed (Bitmap Index/Heap, estimated cost8,54).
Прежний внутренний FK plan не сохранён; точную причинную разницу latency
не утверждаем. Это конкретное изменившееся условие для одного четвёртого
прохода неизменённого frozen helper. Он завершился exit0/stderr0: удалены
4332 instruments, одна sync state, шесть automations и один TEST broker
(4340 строк), deleted точно совпадает с candidates. Первый receipt потерян,
поэтому точную суммарную величину удаления всех проходов не утверждаем.
Критерии фактически выполнены: все четыре purge process/backend отсутствуют,
fresh dry-run во всех 24 таблицах TEST0 и errors0, ошибки исходного TEST scope0,
временный индекс отсутствует. TEST4 stopped/restart=no; PROD4 и shared PG/collector
running, OOM=false/restarts0; image IDs, flags и mounts не изменены, original4PVs
сохранены. Рабочая схема всё ещё 0005.

Публичные /api/health и / возвращают200, health.status=ok и app root присутствует.
Core/Analytics pings200/statusok. Certificate-pinned login/list/GETdetails/logout
прошли: один PROD автомат, details200/shapevalid/IN_WORK/errors0, databaseok и
schema compatible. Исправление URL проверочного helper с flat card на /details
прошло отдельное независимое delta review и mock self-check; приложение не менялось.
Это проверка HTML/API базового функционала, не browser JS rendering или trading
readiness. Полная сверка рабочей финансовой БД не выполнялась.

Размер Core database по pg_database_size: 8 611 200 023 → 7 310 154 775 B
(8,02 → 6,81 GiB, освобождено1,21 GiB). Allocated shared PGDATA — 7,64 GiB,
включая WAL; свободно на root filesystem36,75 GiB. DELETE/ordinaryVACUUM не
обещают полной физической компактизации. Живой PROD tail event/audit сохранён.
Ресурсный снимок13:38UTC: PROD4 около328,0 MiB RAM; со shared PostgreSQL и collector
около652,2 MiB. CPU Docker sample суммарно≈0,81 ядра из4; это разовый замер во
время probes, не доказательство длительной нагрузки.
[Обычный VACUUM PostgreSQL16](https://www.postgresql.org/docs/16/routine-vacuuming.html).

## Проверки и независимое ревью

| Задача | Рецензент | Результат |
|---|---|---|
| TEST helper | test_purge_review | Approved C0/I0/M1; 16 isolated SQLite/PostgreSQL tests |
| Core invariants | core_invariants_review | Approved C0/I0/M0; populated migrations и repository contracts |
| Worker states | worker_states_review | Approved C0/I0/M1 после исправления и повторной проверки двух Important |
| Decision audit | decision_audit_review | Approved C0/I0/M1; 108 targeted tests |
| Lease audit | lease_audit_review_final | Approved C0/I0/M1; 89 targeted + восемь SQLite tests |
| Portfolio index | portfolio_run_index_review | Approved C0/I0/M1; 69 PostgreSQL, 55 SQLite, 13 owned tests |
| Временный purge index | purge_scope_index_review | Approved C0/I0/M0; три isolated PostgreSQL tests |
| Узкое обслуживание и фактический TEST purge | purge_final_ops_review; purge_tail_performance | Approved C0/I0/M0; final I1 закрыт по receipt/zero/drop/fences/health/basic API |
| Итоговые документы и архив | database_integration_review | Approved C0/I0/M0; 39 archive SHA/size, 65 source SHA, claims сверены с direct evidence |

Общий проектный suite и служебный suite запускаются раздельно: совместный сбор
повторно регистрирует PostgreSQL pytest plugin и остановился до выполнения тестов.
Первая общая проверка: проект 2409 passed / 6 failed, служебный набор 127 passed /
9 failed. Пять project failures — невалидные значения PENDING/NEW в старом fake
брокерской заявки; ещё один — запрещённое guard словом в документации, уже
исправленное root. Девять develop failures — устаревшие настройки synthetic
sandbox/TRADE и wrapper, не передающий runtime identity/generation. Отдельно
получены 38 lint замечаний и два Black файла разового purge helper/test; исправление
входит в тот же финальный пакет. Финальный reviewer database_integration_review
первоначально C0/I1/M1; независимое повторное ревью закрыло I1/M1 и одобрило source
C0/I0/M0. Recovery проверен 185 целевыми тестами; старые harness fixtures теперь
передают runtime identity/generation и типизированные transport errors согласно
настоящему CoreClient. TEST known-open заявки продолжают broker-only lookup после
паузы и перезапуска; исходный TEST UNCERTAIN path сохранён. Семантический AST обоих
purge файлов совпадает до/после formatting cleanup. Служебный полный suite: 141 passed / 16 existing warnings;
проектный полный suite: 2437 passed / 232 existing warnings (423,83с). Объединённые Ruff/Black 65 Python файлов и git diff --check прошли. Existing SDK figi, Starlette/httpx, SQLAlchemy flush и
Python SQLite datetime deprecation warnings не маскировались.

Ограничения: стоимость lease checkpoint query на большой истории и production
throughput отдельно не измерялись; frontend не менялся этой задачей. Trading
readiness и длительная ресурсная приёмка O1/S1/M1 остаются открытыми.
Локальные доказательства: `develop/reports/database-improvements-20261011/`;
task-start diffs, briefs и SDD ledger сохранены в локальном
`develop/reports/database-improvements-20261011/sdd-archive/` с проверкой каждого
SHA256. Исходный временный workspace только этого плана удалён; другие планы
и пользовательские изменения сохранены. Все четыре Ruling в исходном порядке
с ценой решений записаны в `develop/reports/database-improvements-20261011/rulings.md`.
Локальный архив не заменяет пользовательский Git commit.

Пароль отдельной локальной тестовой БД, попавший в первоначальный traceback,
отозван; private test.env обновлён, свежий вход проверен. Журнал очищен от значения.
Секреты приложения и рабочей БД не выводились.
