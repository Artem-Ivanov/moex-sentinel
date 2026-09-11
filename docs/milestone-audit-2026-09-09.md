# Актуализация вех по кодовой базе: 2026-09-09

По поручению пользователя саб-агент `milestone_code_audit` сопоставил каждый
нумерованный пункт прежних планов с кодом, тестами, датированными приёмками и
уточнениями постановки. Текущая очередь перенесена в [единый индекс](phase-1-implementation-plan.md),
[фаза 0](phase-0-trading-service-refactor.md) описывает принятый baseline.

В этом аудите не выполнялись торговые операции, миграции или повторная runtime-
приёмка. Наличие теста — карта доказательств поведения; прежние численные результаты
остаются в исходных отчётах с их датами. Проверки ссылок и документации относятся
только к настоящей редакции. Независимый рецензент `core_sandbox_recovery`
проверил полноту 51 пункта, ссылки, архивы и актуальные требования: APPROVED.
На момент пунктовой сверки 0.9.5 выполнялась; затем завершена отдельно,
см. [приёмку](milestone-0.9.5-acceptance-2026-09-09.md). Строки ниже сохраняют
контекст аудита; текущие статусы задаёт единый индекс.

## Решения, ограничивающие план

- Стратегия одна и определяется кодом. Рыночная адаптация порогов не означает
  каталог стратегий в БД. Новые поля бюджета позиции не добавляются.
- Старой БД нет для импорта: clean-slate запуск и bootstrap открытых позиций
  заменили shadow-переход. Действуют baseline/head и один typed runtime.
- Core владеет PostgreSQL, включая отдельный portfolio snapshot worker;
  Worker — SQLite/outbox; Analytics — отдельный процесс без persistence.
- 0.9.3 измеряла синтетический WAIT-профиль с Core SQLite. Эти результаты
  не подтверждают production SLA, PostgreSQL или BUY/SELL burst.
- 0.9.5 обрабатывает недоступность песочницы. Установление внешней причины
  не входит в обязательную приёмку. Последнее уточнение: максимум пять повторов
  после начальной попытки, паузы 1/3/5/7/9 секунд, `SANDBOX_RETRY_LIMIT=5` по
  умолчанию. После исчерпания Core gateway требует изменения конфигурации источника
  или перезапуска; Worker требует перезапуска runtime/процесса. Одно изменение
  конфигурации Core заблокированный Worker не возобновляет.
  Прежние требования экспоненциальной задержки, jitter и неограниченного
  автоматического восстановления заменены этим ограниченным контрактом.

## Пунктовая карта прежней фазы 0

Исходный текст сохранён в [архиве фазы 0](phase-0-trading-service-refactor-archive-2026-09-09.md).
Диапазоны ниже перечисляют однотипные подпункты явно; решение относится ко всем
требованиям соответствующего блока, с отдельно указанными исключениями.

| Прежний пункт | Решение и актуальное требование | Код / проверка / доказательство |
| --- | --- | --- |
| Введение, формат, очередность | Уточнены: фаза 0 закрыта; очередь ведётся в одном индексе. Архивные команды и промежуточные результаты не являются сегодняшними gates. Для существенных правок обязательны подходящие тесты и независимое review; известный дефект нельзя скрывать переименованием категории. | [Текущий план](phase-1-implementation-plan.md), [правила](../AGENTS.md). |
| Общая оговорка о frontend | Удалено устаревшее «функциональные UI-тесты не вводим»: Playwright smoke уже существует. Сохранены API/контрактные проверки; mock и runtime сценарии различаются. | [Браузерная приёмка](milestone-0.9.3-acceptance-2026-09-09.md#ui-и-регрессия). |
| 0.1 | Закрыта. Pydantic для DTO, ORM и lifecycle классы имеют свои обязанности; ноль декораторов dataclass в src. | [Закрытие остатка](milestone-0.9.3-acceptance-2026-09-09.md). |
| 0.1.1, 0.1.2 | Закрыты. Классификация и миграция DTO выполнены; исключение ORM не означает перенос ORM в Pydantic. Ненужные DTO lifecycle заменены обычными классами. | [Core DTO](../src/moex_sentinel/domain), [Worker DTO](../src/trading_automaton/domain), [bootstrap](../src/trading_automaton/services/position_bootstrap.py). |
| 0.1.3, 0.1.4 | Закрыты. Pydantic validation/serialization и совместимость проверяются существующими контрактами. Старый допуск одного падения был промежуточным и исключён из текущих правил. | [Контрактные тесты](../tests/contracts), [приёмка baseline](milestone-acceptance-2026-09-08.md). |
| 0.1.5, приёмка 0.1 | Закрыты. После исполнения OPEN следующая итерация не повторяет заявку; WAIT сохраняет reason code. Старые 533 теста и несовместимости портов оставлены только в архиве. | [Матрица решений](../tests/trading_automaton/services/test_strategy_selection_matrix.py), [benchmark/restart](../develop/tests/test_runtime_benchmark.py). |
| 0.2 | Закрыта. Один путь доставки, явное время, scope и lineage; recovery Worker и проекция Core — намеренно разные представления. | [Приёмка 0.2](milestone-0.2-acceptance-2026-09-09.md), [владение](data-ownership.md). |
| 0.2.0 | Закрыта. Структурно совместимые узкие service/repository порты сохранены. Старые strict mypy по сотням файлов не объявляются повторно проверенными; в приёмке 0.2 указан фактический отдельный запуск. | [Общие порты](../src/trading_automaton/domain/ports.py), [приёмка 0.2](milestone-0.2-acceptance-2026-09-09.md). |
| 0.2.0.1 | Закрыта. IntentUpdatePort и OrderStageAuditPort разделяют повторно используемые узкие обязанности. | [Порты](../src/trading_automaton/domain/ports.py). |
| 0.2.0.2 | Закрыта. Hydration, consistency и audit работают через конкретные типизированные контракты. | [Consistency](../src/trading_automaton/services/position_consistency.py), [тесты](../tests/trading_automaton/services/test_position_consistency_service.py). |
| 0.2.0.3 | Закрыта. Tracking/dispatch/reconciliation сохраняют явные repository контракты. | [Tracking](../src/trading_automaton/services/order_tracking.py), [тесты](../tests/trading_automaton/services/test_order_tracking_service.py). |
| 0.2.0.4 | Закрыта. Синхронизация использует typed outbox и подтверждённое состояние Core. | [Synchronization](../src/trading_automaton/services/fact_synchronization.py), [тесты](../tests/trading_automaton/services/test_fact_synchronization.py). |
| 0.2.0.5 | Архивирована историческая процедура. Текущая приёмка с точными командами и областью проверки хранится отдельно; отсутствие mypy в declared dev dependencies не маскируется старым uv run. | [Фактическая приёмка 0.2](milestone-0.2-acceptance-2026-09-09.md). |
| 0.2.1 | Закрыта. Карта владельцев, ограничения и типы времени актуализированы для текущей baseline. | [Карта данных](data-ownership.md), [schema parity PostgreSQL](../tests/integration/postgresql/test_schema_constraints.py). |
| 0.2.1.1 | Уточнена. Кодовый BrokerApiRegistry и account-scoped user_brokers сохранены. Shadow таблицы, strategy defaults, transfer planner/CLI и временные migration записи исключены из актуального runtime. Transient sandbox action не становится постоянной настройкой. | [Registry](../src/moex_sentinel/adapters/broker_api_registry.py), [baseline](../alembic/versions), [тесты registry](../tests/adapters/test_broker_api_registry.py). |
| 0.2.1.1.1 | Заменена clean-slate. Исправление one-to-many legacy mapping сохраняется только как история; повторного импорта source/target mappings нет. | [Приёмка clean-slate](milestone-acceptance-2026-09-08.md). |
| 0.2.1.2 | Уточнена. Нормализованные факты, typed records, UoW, scoped FK, CAS и rollback действуют в текущей схеме. Shadow heads, суффикс v2 и утверждение об отключённых production adapters устарели. | [Repositories](../src/moex_sentinel/storage/repositories), [PostgreSQL facts](../tests/integration/postgresql/test_trading_facts.py). |
| 0.2.1.3 | Уточнена. Один typed ingress/outbox, batch, selective ACK и exact replay сохранены. FACT_INGRESS_VERSION, legacy endpoint и выбор между двумя протоколами удалены из действующего плана. | [Envelopes](../src/sentinel_contracts/trading_facts.py), [ingress](../src/moex_sentinel/services/trading_fact_ingress.py), [sync tests](../tests/trading_automaton/services/test_fact_synchronization.py). |
| 0.2.1.4, 0.2.1.5, 0.2.1.6 | Заменены clean-slate: нет задачи импортировать старую историю, переключать legacy repositories или удалять их таблицы. Сохранять прежние volumes до отдельного разрешения; штатная forward migration текущего volume допустима. | [Приёмка запуска](milestone-acceptance-2026-09-08.md), [схема и запуск](development.md). |
| 0.2.2 | Закрыта с уточнением. Реальные typed контракты заменили проектные названия FactSink/FactReader; создавать дополнительные интерфейсы ради совпадения имён не требуется. Запрещён независимый dual publish, но локальные durable записи Worker необходимы. | [Core ports](../src/moex_sentinel/services/trading_fact_ports.py), [владение](data-ownership.md). |
| 0.2.3 | Закрыта с уточнением. Scope/lineage, rollback/replay и источник состояния проверены. Общее требование «без AUDIT-шума» заменено точным запретом повторной заявки: WAIT остаётся историей решений. | [Ingress tests](../tests/services/test_trading_fact_ingress.py), [приёмка 0.8](milestone-0.8-acceptance-2026-09-09.md). |
| 0.3, 0.3.1 | Закрыты. Состояния и переходы определяет общая политика с инициатором, включая bootstrap и терминальный CLOSED. | [Политика](../src/sentinel_contracts/automation_lifecycle.py), [тесты](../tests/sentinel_contracts/test_automation_lifecycle.py). |
| 0.3.2 | Закрыта. Команды, статус и факты пересекают границу только через контракт. | [Два сервиса](../tests/integration/test_core_worker_lifecycle.py). |
| 0.3.3 | Закрыта. Корреляция этапов исполнения и восстановления сохранена. | [Приёмка 0.3](milestone-0.3-acceptance-2026-09-09.md). |
| 0.4, 0.4.1 | Закрыты. Отдельный PostgreSQL, health/schema check, явный one-shot migration job. | [Compose](../compose.yml), [миграционная команда](../src/moex_sentinel/migrations/schema.py). |
| 0.4.2 | Уточнена. PostgreSQL получают backend, portfolio snapshot worker и миграционный job внутри Core; торговый Worker — только SQLite. | [Compose](../compose.yml), [SQLite guard](../tests/trading_automaton/storage/test_worker_database.py). |
| 0.4.3 | Заменена clean-slate. Удалённая moex-migrate-core-database и её chunk transfer не являются инструкцией запуска; исходная SQLite не используется как runtime rollback. | [Действующий setup](development.md), [приёмка baseline](milestone-acceptance-2026-09-08.md). |
| 0.4.4 | Закрыта. Внешний Worker volume сохраняет cache, intent и outbox независимо от Core. | [Compose](../compose.yml), [тест Worker DB](../tests/trading_automaton/storage/test_worker_database.py). |
| 0.4.5, 0.4.6 | Не определены в исходном документе, хотя заголовок обещал диапазон 0.4.1–0.4.8. Не создаём отсутствовавшие требования и не утверждаем их приёмку. | [Архив](phase-0-trading-service-refactor-archive-2026-09-09.md). |
| 0.4.7 | Уточнена и закрыта. Документация описывает текущий setup, forward migration и сохранение volumes; legacy transfer исключён. | [Разработка](development.md), [brief](../AGENT_BRIEF.md). |
| 0.4.8 | Уточнена. Приёмка текущего Compose выполнена; повторный rehearsal возврата Core на legacy SQLite исключён после смены архитектуры. Проверки нового обновления выполняются для реально меняющихся сервисов. | [Приёмка baseline](milestone-acceptance-2026-09-08.md), [приёмка Core 0.2](milestone-0.2-acceptance-2026-09-09.md). |
| 0.5, 0.5.1 | Закрыты. Core stream/history → Analytics market-only → Worker decisions; у Analytics нет credentials, портфеля и persistence. | [Analytics service](../src/market_analytics/service.py), [приёмка 0.5](milestone-0.5-acceptance-2026-09-09.md). |
| 0.5.2 | Закрыта. JSON/Pydantic batch одного поколения, timestamp/TTL; необязательное слово proto из прежнего плана не создаёт задачу внедрить protobuf transport. | [Контракт](../src/sentinel_contracts/analytics.py), [тесты](../tests/contracts/test_analytics.py). |
| 0.5.3 | Закрыта. Stale запрещает новые активные действия, fresh допускает обработку; проверка непрерывной доступности провайдера не требуется для завершённой функциональной вехи. | [Worker contract](../tests/integration/test_analytics_worker_contract.py), [runtime tests](../tests/trading_automaton/test_analytics_runtime.py). |
| 0.6, 0.6.1 | Уточнены и закрыты. UI управляет подключениями доступных кодовых адаптеров. Backend валидирует бизнес-поля; frontend имеет обычную валидацию ввода и отображение ошибок. Произвольное добавление адаптеров/endpoint через UI не обещается. | [API](../frontend/src/api/brokers.ts), [форма](../frontend/src/components/BrokerForm.vue). |
| 0.6.2 | Закрыта. Контракты, счета, навигация, mock Chrome/Playwright и runtime GET-only проверены, включая мобильную ширину. | [Приёмка UI](milestone-0.9.3-acceptance-2026-09-09.md#ui-и-регрессия). |
| 0.7, 0.7.1 | Закрыты в уточнённой пользователем постановке. Единые код/версия алгоритма; каталога шаблонов и assignment migration нет. | [Identity](../src/sentinel_contracts/strategy.py), [приёмка 0.7](milestone-0.7-acceptance-2026-09-09.md). |
| 0.7.2 | Закрыта. Общие лоты покупки, свободные средства и intent reserves сохранены; индивидуальные бюджеты не введены. История решения сохраняет snapshot и indicators, новый расчёт её не меняет. | [Стратегия](trading-strategy.md), [сквозная история](../tests/integration/test_dynamic_strategy_contract.py). |
| 0.7.3 | Закрыта. Изменение волатильности, fallback при потере истории, возвращение истории и restart проверены; readiness SMA и fresh gate остаются отдельными ограничениями. | [Сквозные тесты](../tests/integration/test_dynamic_strategy_contract.py), [приёмка 0.7](milestone-0.7-acceptance-2026-09-09.md). |
| 0.8, 0.8.1 | Закрыты. Подтверждённый вход и согласованный рыночный контекст реализованы; стратегия не заменяется новой из-за изменения плана. | [Матрица](../tests/trading_automaton/services/test_strategy_selection_matrix.py), [приёмка 0.8](milestone-0.8-acceptance-2026-09-09.md). |
| 0.8.2 | Закрыта. Усреднение, net profit с комиссиями и ценовая семантика stop-loss определены текущей спецификацией; оптимизация только после профиля. | [Стратегия](trading-strategy.md), [adaptive tests](../tests/trading_automaton/services/test_adaptive_scalping_strategy.py). |
| 0.8.3 | Уточнена и закрыта. Матрица BUY/SELL/WAIT и stale работает; идемпотентность означает отсутствие повторного исполнения/факта, а не запрет нового наблюдения WAIT на следующем такте. | [Execution graph](../tests/integration/test_execution_cycle_core_contract.py), [atomic cycle](../tests/trading_automaton/storage/test_execution_cycle_integration.py). |
| 0.9 | Уточнена. Дополнительные пункты имеют реальные статусы и условия, а не общий список будущей работы. | [Текущая очередь](phase-1-implementation-plan.md). |
| 0.9.1 | Поглощена 0.5. SDK push уже подписывает order book depth=20, last price, trading info и закрытые 1m свечи. Новая задача «полного streaming» не нужна; расширенная глубина не обещана. | [Подписки](../src/moex_sentinel/adapters/tinvest/streaming.py). |
| 0.9.2 | Условно отложена. Возможная замена только Worker SQLite требует доказанного ограничения; Core уже PostgreSQL. | [Worker guard](../src/trading_automaton/config.py), [ограничения benchmark](milestone-0.9.3-acceptance-2026-09-09.md). |
| 0.9.3, DoD 0.9.3 | Закрыты с границами. 6/20/50, p50/p95/p99 и replay после потери ACK; временные SQLite и WAIT после начальных BUY/FILLED. Не production SLA, PG профиль или burst заявок. | [Benchmark](../develop/benchmarks/README.md), [приёмка](milestone-0.9.3-acceptance-2026-09-09.md). |
| 0.9.4 | Уточнена и запланирована. Сначала профиль доставки на отдельном Core PostgreSQL, затем обоснованный объём оптимизаций. До задания целевой нагрузки нельзя утверждать достижение p95/p99 SLA. | [Критерии следующей вехи](phase-1-implementation-plan.md#следующая-веха-094--профиль-доставки-фактов-на-core-postgresql). |
| 0.9.5 | В работе. После начальной попытки до пяти повторов с паузами 1/3/5/7/9 секунд (лимит настраивается), затем Core gateway останавливается до изменения конфигурации источника/перезапуска, Worker — до перезапуска runtime/процесса. Безопасное ожидание и наблюдаемость без обязательного расследования внешней причины. Экспонента/jitter заменены последним уточнением. Аудит не закрывает реализацию. | [Постановка 0.9.5](milestone-0.9.5-sandbox-resilience.md). |

## Пунктовая карта прежнего плана следующей фазы

Исходный файл представлял историю нескольких приёмок. Он сохранён в
[архиве прежней фазы 1](phase-1-implementation-plan-archive-2026-09-09.md).

| Прежний блок / пункт | Решение и актуальное место |
| --- | --- |
| Заголовок «завершённая 0.9.3», bootstrap/UI/WAIT fix | Сохранены как завершённые результаты в [приёмке 0.9.3](milestone-0.9.3-acceptance-2026-09-09.md), не текущая задача. |
| «Следующая 0.9.5» и наблюдение UNAVAILABLE | Статус уточнён до «в работе»; исторические десятиминутные измерения не превращены в текущий runtime gate. [Полная постановка](milestone-0.9.5-sandbox-resilience.md). |
| Профиль Core PostgreSQL перед оптимизацией | Вынесен в явную следующую 0.9.4 с критериями и границами. |
| Завершённая 0.2 | Сохранена в [приёмке 0.2](milestone-0.2-acceptance-2026-09-09.md), повторное завершение не требуется. |
| Абзацы 0.3, 0.8, 0.5, 0.7 | Сведены к завершённому [baseline](phase-0-trading-service-refactor.md) и исходным приёмкам; все уточнения стратегии и ограничение свежести сохранены. |
| Clean-slate: статус и операционный запуск | Датированная история остаётся в архиве и [приёмке](milestone-acceptance-2026-09-08.md). Число автоматов на дату запуска не заявляется текущим. |
| Clean-slate результат 1 | Закрыт: одна baseline→head цепочка; текущие forward migration сохраняют торговые данные. [Схема](development.md). |
| Clean-slate результаты 2, 3 | Закрыты: подключение/счёт и каталог через штатный API/UI, принятие положительных позиций через HOLD/BOOTSTRAPPING. [Bootstrap e2e](../tests/integration/test_open_position_bootstrap.py). |
| Clean-slate результаты 4, 5, 6 | Закрыты: атомарный cycle/lot/outbox, IN_WORK последним фактом, дальнейшие решения. [Bootstrap e2e](../tests/integration/test_open_position_bootstrap.py), [lifecycle e2e](../tests/integration/test_core_worker_lifecycle.py). |
| Clean-slate результат 7 | Постоянный инвариант: прежние volumes не удаляются без явного разрешения. Не является незавершённой задачей очистки. [Правила](../AGENT_BRIEF.md). |
| Clean-slate результаты 8, 9 | Закрыты: отдельный snapshot worker и forward migration без потери торговых данных. [PostgreSQL migration tests](../tests/integration/postgresql/test_portfolio_snapshot_schema_migration.py). |
| Ссылки на checklist и closeout plan | Сохранены в исходной приёмке/архиве; runbook применяют по нужной операции, а не запускают весь clean-slate повторно при продолжении работы. |

## Проверка полноты и границы редактирования

Все определённые нумерованные заголовки прежней фазы 0 и пункты 0.9.1–0.9.5
имеют строку в матрице. Дополнительно разобраны упомянутые 0.2.1.1.1,
0.2.1.4–0.2.1.6 и отсутствующие 0.4.5/0.4.6, чтобы не создавать ложных задач
или свидетельств приёмки. Все девять результатов clean-slate отражены отдельно
либо явными группами.

Архивы оставлены в той же директории, поэтому относительные ссылки сохраняют
свои цели. Сохранён действующий якорь 0.7; прежняя входящая ссылка на lifecycle
1.6 ведёт к пояснению замены в новом индексе. Прочие исторические specs, планы
и acceptance не переписывались. При противоречии старого плана приоритет имеют
уточнения пользователя, текущая архитектура и этот пунктовый разбор.
