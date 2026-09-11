# Оценка кодовой базы и очередь улучшений — 2026-09-10

## Вывод и граница оценки

Следующая веха — **[0.9.8: ограниченная по памяти доставка outbox и корректные повторы](../milestone-0.9.8-outbox-delivery.md)**.
В ней объединены два изменения одного пути доставки: устранение воспроизведённого
дефекта durable retry и ограничение материализации накопленной очереди Worker.
Остальные результаты ниже остаются отдельными кандидатами в
[единственном актуальном плане](../phase-1-implementation-plan.md).

Проведён целевой обзор Core/storage/API, Worker/outbox/tracking, Analytics и
асинхронного поведения frontend. Это оценка выбранных путей и существующих тестов,
а не исчерпывающий аудит каждой строки. Production-код этим документом не изменён;
найденные дефекты ещё не исправлены. Рабочие БД, сеть и контейнеры при оценке не использовались.

Архитектурная основа пригодна для дальнейшего развития: разделены владельцы
хранилищ, есть composition roots, typed DTO и порты, узкие views/usecases,
контрактные проверки SQLite/PostgreSQL, тесты реального UoW и frontend-компонентов.
Основные пробелы — сценарии взаимодействия во времени, неограниченные чтения
очереди и повторные загрузки связанных записей. Переписывание архитектуры целиком
или дробление файлов исключительно по размеру этим обзором не обосновано.

## Доказательства и ограничения

| Проверка | Результат |
| --- | --- |
| Свежая статическая проверка `ruff check src tests --statistics` | Exit 0, замечаний нет. |
| Свежая проверка `black --check src tests` | 380 файлов без изменений, exit 0. |
| Свежий `npm --prefix frontend test` | 74 теста, 19 файлов, пройдены. |
| Свежий `npm --prefix frontend run typecheck` | Exit 0. |
| Предшествующая полная регрессия 0.9.7, в тот же день | 1152 passed с отдельным PostgreSQL; для документального аудита повторно не запускалась. |
| Подтверждение W1 | Проба в памяти с существующими стабами и движущимися часами: 6 транспортных ошибок, 5 пауз 1/2/4/8/16 секунд, retry назначен на 30 секунд в прошлом. |

[Приёмка 0.9.7](../milestone-0.9.7-acceptance-2026-09-10.md) остаётся действующей:
SQL на WAIT уменьшился с 102/103/106 до 93/94/97 для 6/20/50 инструментов.
Устойчивое ускорение полной цепочки не доказано. Основной профиль 20/50 был хуже
исторического; разнонаправленный ABBA на 20 инструментах и 30 ticks не снимает
вопрос о нагрузке 50/100. Причина изменения общей latency не установлена.

Текущий последовательный benchmark не измеряет конкуренцию HTTP-запросов.
В [pg_metrics.py](../../develop/benchmarks/pg_metrics.py) и
[pg_server.py](../../develop/benchmarks/pg_server.py) атрибуция ingress
использует общий флаг `active_ingress`: перед конкурентным профилем потребуется
изоляция метрик по запросу. Остаточное время за вычетом SQL нельзя автоматически
называть CPU: отдельно не выделены все ожидания, ORM и сериализация.

Приоритеты: **P1** — ближайшие отдельные задачи надёжности/отзывчивости;
**P2** — следующий обоснованный объём оптимизации или защиты;
**P3** — сопровождение либо гипотеза после измерения. Размер S/M — относительная
сложность малого/среднего изменения, не оценка в днях. Очерёдность учитывает также
доказательность, границы изменения и возможность независимой приёмки.

## Worker и Analytics

| ID / приоритет / размер | Место и сценарий | Исправление и критерий проверки |
| --- | --- | --- |
| **W1 / P2 / S**, входит в 0.9.8 | [fact_synchronization.py](../../src/trading_automaton/services/fact_synchronization.py), `flush_outbox`, `_apply_result`, `_schedule_rows`: время берётся до HTTP и используется после длительных повторов. **Дефект воспроизведён:** следующий durable retry уже просрочен, новая серия начинается без положенной паузы. | Получать актуальное время при назначении durable retry, включая долгий ответ с retryable group failure. Fake clock растёт в HTTP/sleep; due отсчитывается после попыток. Сохранить внутренние паузы, retry counters, cap 60 секунд, envelopes и selective ACK. Это доставка фактов, отдельная от sandbox retry 0.9.5. |
| **W2 / P2 / M**, входит в 0.9.8 | [repository.py](../../src/trading_automaton/storage/repository.py), `ready_fact_outbox` / `_fact_publication_units`: `.all()` загружает весь PENDING с JSON, затем все cached bootstrap-автоматы, группирует и сортирует в Python; limit действует в конце. **Факт по коду; величина влияния на latency пока не измерена.** | Ограничить одновременно удерживаемые метаданные и payload, выбирать bootstrap-контекст нужных кандидатов. До изменения — профиль 100/10 000/50 000 фактов, затем сравнение состава batch с прежним алгоритмом на одном dataset. Проверить blocked prefix, deadline, quartet, ACK/restart и полный drain. Подробные инварианты — в спецификации вехи. |
| **W3 / P2 / M** | [order_tracking.py](../../src/trading_automaton/services/order_tracking.py), failure/containment paths: некоторые `update_intent`/`hold_active` синхронно вызываются из async; [database.py](../../src/trading_automaton/storage/database.py) задаёт SQLite busy timeout 5000 ms. Возможное ожидание lock занимает event loop. **Длительность блокировки и последствия в runtime не измерены.** | Последовательно ожидать перенос целой локальной операции в поток, как уже сделано для finalization/audit. Тест блокирующим repository stub: независимый heartbeat работает, порядок UNCERTAIN → cash → HOLD сохраняется. Не распараллеливать одну транзакцию. |
| **W4 / P3 / M** | [repository.py](../../src/trading_automaton/storage/repository.py), atomic finalization около строк 630/653 и ledger около 1716/1779: дублируются opening/LIFO allocation, комиссии и PnL. В [order_tracking.py](../../src/trading_automaton/services/order_tracking.py) сохранённый `_ledger` не используется. **Расхождения формул сейчас не найдено.** | Небольшие общие helpers с переданной Session и единым владельцем транзакции; удалить неиспользуемую зависимость. Оба входа дают одинаковые allocations при BUY/SELL, partial, равных timestamps, комиссиях и недостатке лотов. Проверить atomic finalization/outbox и replay. |
| **W5 / P3 / M**, условно | [service.py](../../src/market_analytics/service.py), [indicators.py](../../src/market_analytics/indicators.py): повторные фильтрация/сортировка до 120 свечей и Decimal-расчёты при неизменной истории. **Польза кэша — гипотеза.** | Сначала CPU-профиль 6/20/50 инструментов. Только при существенной доле — ограниченный кэш чистых metrics с учётом источника, инструмента, исправлений свечей и fallback. Не кэшировать freshness/snapshot: TTL, captured_at и границы возраста вычисляются заново. |

## Core, SQL и HTTP

| ID / приоритет / размер | Место и сценарий | Исправление и критерий проверки |
| --- | --- | --- |
| **C1 / P2 / M** | [order_facts.py](../../src/moex_sentinel/storage/repositories/order_facts.py), `_require_common_lineage`; [trading_audit.py](../../src/moex_sentinel/storage/repositories/trading_audit.py), `append_audit`: после scoped existence повторно читаются те же ссылки для lineage. По веткам кода common lineage с cycle — 5 SELECT, audit с полными optional refs — 9. | Прочитать необходимые поля один раз внутри repository-операции. Расчётный бюджет 5→3 и 9→5 — **не измеренная экономия WAIT**. Сохранить приоритет всех scoped errors перед lineage errors, nullable references, autoflush, видимость вставок группы и rollback. Матрица нескольких неверных ссылок, SQLite/PostgreSQL SQL budget и профиль. |
| **C2 / P2 / M** | [automations.py](../../src/moex_sentinel/storage/repositories/automations.py), `get_many`, `list_active`, `_record_for_model`: дополнительные broker/instrument/cycle reads на автомат. **N+1 подтверждается кодом**, часть broker reads может поглотить identity map. | Scoped JOIN с LEFT JOIN cycle либо фиксированное число пакетных чтений. Тест 0/1/50 автоматов, разных брокеров, отсутствующего cycle/ID, дублей ID и стабильного порядка `(created_at,id)`. Не скрывать некорректные ссылки INNER JOIN, если прежний контракт выдавал ошибку. Это read model, не WAIT ingress. |
| **C3 / P2 / S–M** | [order_facts.py](../../src/moex_sentinel/storage/repositories/order_facts.py), replacement; [position_ledger.py](../../src/moex_sentinel/storage/repositories/position_ledger.py), cycle replacement: UPDATE RETURNING id, затем чтение полной записи. | Возвращать нужные поля UPDATE RETURNING: потенциально минус один SELECT на успешную замену каждого типа. Предварительные проверки пока сохранить. Проверить scope/lineage, immutable intent, UTC, rollback и актуальность заранее загруженной ORM-модели в одной Session. |
| **C4 / P2 / M**, после профиля | [trading_summary.py](../../src/moex_sentinel/services/trading_summary.py) и [portfolio_snapshots.py](../../src/moex_sentinel/storage/repositories/portfolio_snapshots.py), `common_baselines`: три отдельных окна 24h/7d/30d повторно выбирают run и snapshots. | Измерить длинную синтетическую историю; совместно выбирать baseline IDs и переиспользовать одинаковые runs. Сохранить общий run всех текущих счетов валюты, fallback, частичную историю и изменение состава счетов. EXPLAIN и query budget; не подменять общую базу независимыми latest snapshots. |
| **C5 / P2 / S–M** | [portfolio_snapshot_collection.py](../../src/moex_sentinel/services/portfolio_snapshot_collection.py), `_operations`: нет защиты от повторного cursor. При ответах A→A либо A→B→A цикл не завершается, удерживая общий collection lock; stop ожидает collect_once. **Условный сценарий из кода, не обнаруженный runtime-инцидент.** | Повторные cursors, явные пределы страниц/времени и типизированная ошибка счёта; не сохранять частичные операции как полные. Тесты корректной длинной истории, следующего счёта после ошибки, освобождения lock и ограниченного shutdown. |
| **A1 / P1 / M** | [internal_trading_facts.py](../../src/moex_sentinel/views/internal_trading_facts.py), [automations.py](../../src/moex_sentinel/views/automations.py), [health.py](../../src/moex_sentinel/views/health.py): async views напрямую исполняют синхронный usecase/SQLAlchemy. **Блокирование loop следует из кода**, влияние на параллельные HTTP-запросы требует измерения. | Целый синхронный usecase/UoW выполнять вне event loop через ограниченный executor или sync-view. Session создаётся и закрывается в том же потоке; настоящие async-ветки сохраняются. Конкурентный HTTP-тест, CAS/replay одного автомата, rollback, audit context и лимит занятых потоков. Перед конкурентным benchmark исправить атрибуцию метрик по запросам. |

Не удалять второй idempotency lookup в `append_idempotent` лишь по сходству:
ingress lookup ограничен scope, repository проверяет глобальную идентичность
события и имеет самостоятельный контракт при гонках. Межфактовый кэш требует
другого доказательства актуальности; в C1 он не предлагается.

## Frontend и загрузка деталей

| ID / приоритет / размер | Место и сценарий | Исправление и критерий проверки |
| --- | --- | --- |
| **F1 / P1 / S** | [PositionDetailsView.vue](../../frontend/src/views/PositionDetailsView.vue), [TradingSessionStatus.vue](../../frontend/src/components/TradingSessionStatus.vue): timer создаётся после await refresh. Unmount во время загрузки очищает ещё не созданный timer, поздний ответ запускает polling после ухода. У status также нет защиты от перекрытия долгих запросов. | Lifecycle guard, максимум один polling request в работе. Deferred fetch → unmount → resolve/reject → продвижение часов не создаёт запросов; долгий запрос не перекрывается следующим. Использовать существующий подход InstrumentItem. |
| **F2 / P2 / S–M** | [InstrumentsListView.vue](../../frontend/src/views/InstrumentsListView.vue), [PositionsListView.vue](../../frontend/src/views/PositionsListView.vue): старый ответ может перезаписать данные/ошибку/loading нового фильтра; аналогично смене лимита операций в списке торговли. | Результаты чтения привязать к поколению запроса, при необходимости отменять fetch. Тест ответов в обратном порядке, включая ошибки и loading. Не игнорировать итог изменяющего запроса синхронизации каталога. |
| **F3 / P2 / M** | [InstrumentItemView.vue](../../frontend/src/views/InstrumentItemView.vue), [PositionDetailsView.vue](../../frontend/src/views/PositionDetailsView.vue), [BrokerAccountsView.vue](../../frontend/src/views/BrokerAccountsView.vue): mount-only загрузка при переиспользовании RouterView. Возможны старые данные A и действие по текущему route B. **Частота перехода в пользовательском потоке не проверена.** | Реактивная смена контекста по IDs либо осмысленный remount, сброс формы и отбрасывание старого ответа. Настоящий RouterView в тесте A→B, поздний ответ A: все данные и параметры действия относятся к B. |
| **F4 / P3 / S–M**, после измерения | [usecases/automations.py](../../src/moex_sentinel/usecases/automations.py), details: запрос свечей начинается после ожидания операций, хотя они зависят лишь от уже прочитанного автомата. | Замерить обе ветки; при существенной пользе выполнять независимо с сохранением частичных ошибок, внешних лимитов и закрытия ресурсов при cancellation. |

## Решение о следующем объёме и кросс-ревью

0.9.8 выбирается по связности W1/W2 и проверяемости результата: есть воспроизведённый
дефект повтора и явно неограниченная материализация. Это не утверждение, что Worker
объясняет latency 0.9.7 или что найденное место — главный bottleneck эксплуатации.
A1 и F1 получают ближайшие самостоятельные позиции после этой вехи; их нельзя
потерять среди условных SQL/Analytics-оптимизаций. Они не включены в 0.9.8, поскольку
имеют другие контракты конкуренции и независимую приёмку.

Независимые участники оценки:

- `core_improvement_audit` (Nash): Core repositories, SQL, lineage, summary/collector.
- `worker_improvement_audit` (Wegener): Worker storage/outbox/tracking, Analytics;
  воспроизведение W1 в памяти и проверка границ 0.9.8.
- `next_scope_review` (Carson): API, frontend, тесты и независимое итоговое ревью документов.

Проверялись соответствие контрактам, SOLID без лишних слоёв, простота потоков
управления, ресурсы и достаточность поведенческих тестов. Итоговое ревью новых
документов `next_scope_review`: **APPROVED, существенных замечаний нет**.
Редакционное замечание о месте изменения лимита операций F2 исправлено.
Проверены 105 относительных ссылок в пяти новых/обновлённых документах;
`pytest tests/test_documentation.py tests/test_architecture.py -q` — 6 passed.
Найденные проблемы кода являются входом очереди работ; прохождение ревью плана
не означает их исправления.
