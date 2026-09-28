# Прикладной DDD/SOLID аудит

Дата: 2026-09-16. Автор: `/root/fix_delivery`. Анализ текущего дерева после C01–C16 и ponytail W1–W3, без изменений production/tests/runtime. Это аудит реальных границ ответственности, а не предложение перейти на новый архитектурный framework.

## Область и ограничения

Прочитаны `docs/architecture.md`, `docs/data-ownership.md`, ADR0004/0005, текущие Core lifecycle/details/portfolio/fact ingress/UoW, Worker broker iteration/hydration/finalization и его composition, Analytics usecase/calculator, frontend details/API boundary. SQL-схему независимо исследует другой агент: этот отчёт не заменяет проверку FK/index/EXPLAIN. Количества обращений ниже выведены из исходников, не являются измеренным latency. Нового runtime обследования и вызовов брокера не было.

## Реальные границы и инварианты

| Контекст / операция | Владелец и агрегатная граница | Оценка |
| --- | --- | --- |
| Core lifecycle | `AutomationService` проверяет общую transition policy, repository применяет expected revision | Сервис с DTO и чистой policy не является проблемной «анемией»: инвариант явно задан и CAS сохраняется |
| Core fact ingress | `TradingFactIngressService._publish_group` + `TradingFactsUnitOfWork`: automation, последовательность, связанные order/lot/cycle/audit изменения в одной транзакции | Это полезная согласованная граница. Нельзя делить транзакцию по таблицам ради маленьких repositories; группы разных автоматов намеренно независимы |
| Worker исполнение | `LocalAutomationRepository.finalize_execution`: terminal intent, lot allocations, cycle, execution facts/outbox под одной транзакцией | Большой repository не повод переносить записи в несколько services с отдельными commit. Извлекать допустимо чистые вычисления, сохраняя Session/CAS/replay |
| Worker решение | `RunBrokerIterationUsecase`: один владелец commands/last_seen; runtime владеет lock/event/client | Недавнее выделение конечной операции достаточно. Не нужен интерфейс для каждого внутреннего шага |
| Portfolio snapshots | Core collector usecase/store: глобальный run lock отдельно от коротких data Session | Чтение брокера вне data Session и атомарное сохранение snapshot run уже исправлены; не повторять рефактор |
| Analytics | `CalculateAnalyticsSnapshotUsecase` получает market-only snapshot; `AnalyticsService.calculate` считает без I/O | Правильная изоляция credentials/постоянного состояния. Clock используется после await, TTL/candle cutoff остаются частью контракта |
| Shared contracts | `sentinel_contracts`: DTO, lifecycle/market validation без импортов Core/Worker/Analytics | Shared kernel оправдан одинаковыми межпроцессными инвариантами. ORM/domain aggregates не нужно переносить туда |
| Frontend | `api/automations.ts` транспорт, `PositionDetailsView.vue` отображение и poll lifecycle | Торговых решений в UI нет. Decimal→Number используется для отображения chart; не переносить денежные расчёты на JavaScript Number |

## Findings

### A1 — существенное: internal/external instrument ID перепутаны в операциях позиции

**Файлы/символы:** `src/moex_sentinel/usecases/automations.py::ViewTradingAutomationDetailsUsecase.execute` (вызов operations, около155); `services/portfolio.py::PortfolioAggregationService.view_position_operations` (около111–129); `adapters/tinvest/portfolio.py::TInvestPortfolioAdapter.get_operations` (около121–134).

**Факт:** usecase передаёт `automation.instrument_id` — внутренний Core catalog ID. Portfolio service передаёт строку без преобразования в SDK `GetOperationsByCursorRequest.instrument_id`. Каталог явно имеет отдельное `external_instrument_id`. Исправленный candles path уже выполняет scoped resolution, operations path — нет.

**Сценарий:** при разных internal/external UUID запрос операций адресует брокеру неправильный инструмент; details возвращает operations error/пустой результат вместо истории нужной позиции. Неправильный аргумент доказан исходниками, точный ответ реального SDK в этой проверке не измерялся.

**Минимальное исправление:** разрешать внутренний ID через существующий scoped catalog `get(broker_id, internal_id)` на границе position-operation service и передавать adapter только external ID. Документировать направление ID у соответствующего узкого порта. Не добавлять общий resolver framework, не менять generic broker API и не делать fallback «не найдено → использовать internal как external».

**Проверки:** расширить `tests/integration/test_position_details_candles.py`: разные UUID, operations fake отвергает неверный ID (нынешний fake его игнорирует); existing portfolio service/adapter tests, disabled/environment checks, missing/cross-scope catalog record без broker I/O. Сохранить независимую ошибку operations при успешно полученных candles. Отдельный design review до реализации.

### A2 — среднее: N catalog round trips для операций, включая повторные инструменты

**Файлы/символы:** `services/portfolio.py::_read_operations/_with_ticker`; `storage/repositories/reference_catalog.py::find_by_external_instrument_id` (164).

**Факт:** каждый отображаемый operation с непустым instrument ID вызывает отдельный repository lookup; каждый lookup открывает Session и выполняет SELECT. При N eligible операций это N SQL запросов, даже когда все относятся к одному инструменту. Вызовы синхронные внутри async service. Обращения выполняются до финального общего `[:limit]` по всем брокерам/счетам.

**Последствие:** page latency и event-loop blocking растут с числом операций; численное ускорение ещё не измерено.

**Минимальный первый шаг:** request-local memo по `(broker_id, external_id)`, включая negative results, если benchmark подтверждает повторяемость. Это уменьшает N до U уникальных ключей без расширения портов. Existing catalog `list` читает весь каталог и не является автоматически лучшей bulk заменой. Узкий lookup-many оправдан только измерением U и объёма каталога; persistent cache пока не нужен.

**Проверки:** один счёт с N операциями/U инструментами, SQL counter и wall time на isolated DB; до N lookups, после U, результаты/порядок/лимит/errors идентичны. `tests/services/test_portfolio_aggregation_service.py`, `tests/services/test_portfolio_contract.py`. Деньги и timestamps не изменять.

### A3 — среднее, риск требует воспроизведения: hydration собирает durable state несколькими снимками

**Файлы/символы:** `trading_automaton/services/position_state_hydration.py::_hydrate_one/_load_durable`; `storage/repository.py::{has_pending_fact_outbox,intent_history,list_open_lots,get_cycle_state,get_active_intent,realized_pnl}`; `services/lot_ledger.py::reconcile`.

**Факт:** успешный обычный путь имеет отдельный outbox SELECT и пять repository reads, каждый в своей Session; consistency затем повторно читает open lots. Таким образом, до audit/прочих проверок — минимум семь SELECT/Session scopes на автомат. `get_cycle_state` дополнительно может создавать строку и commit. `intent_history` читает все FILLED intents и заново восстанавливает текущий цикл в Python. Hydration разных автоматов запускается через gather/to_thread.

**Риск:** execution watcher может финализировать исполнение между чтениями; cache тогда потенциально объединяет history одного момента с lots/cycle другого. Это пока не доказанная ошибочная сделка: последующие gates/CAS/reconciliation могут её остановить. Стоимость истории растёт с жизнью автомата, даже при небольшом числе открытых лотов.

**Следующий шаг:** barrier-based integration test с finalization между reads и SQL counter на realistic history. Лишь после результата рассмотреть один repository method «прочитать durable position snapshot» в короткой транзакции, с явным handling отсутствующего cycle. Не держать Session через portfolio await, не удалять outbox/active-intent checks и не переносить now раньше snapshot. Не вводить aggregate cache без invalidation contract.

**Проверки:** hydration service, execution-cycle integration, net-position facts, delayed-fill/replay guards. Оценивать как корректность, так и count/latency; параллельный LIFO аудит не дублировать.

### A4 — среднее: неожиданная ошибка ingress теряет техническую причину

**Файл/символ:** `moex_sentinel/services/trading_fact_ingress.py::publish`, `except Exception`.

**Факт:** исключение переводится в безопасный retryable `TEMPORARY_CORE_FAILURE`, но в данном пути нет логирования причины. HTTP успешно возвращает результаты групп, поэтому middleware обычно не видит исключение.

**Сценарий:** постоянный программный дефект получает тот же retryable ответ, что временная недоступность; Worker повторяет outbox, оператор не получает stack trace из этой границы. Это затрудняет расследование, даже если атомарность сохранена.

**Минимальное исправление:** локальное structured error logging причины на границе группы без payload/credentials. HTTP safe code и partial-success semantics не менять. Не заводить новую error hierarchy/observability service. Проверить отсутствие leakage, rollback группы и успешную соседнюю группу существующими ingress tests; учесть log sampling, если retry создаёт шум.

### A5 — низкое: порты не полностью отделены от adapter/storage типов ошибок

**Файлы:** `services/portfolio.py` импортирует `TInvestAdapterError`; `usecases/automations.py` импортирует exceptions из storage repositories; `services/lot_ledger.py` типизирует dependency конкретным `LocalAutomationRepository`.

**Конкретная цена:** второй portfolio adapter должен конструировать TInvest-named error либо service не сохранит partial error semantics; подмена repository требует знания его package exceptions. Это реальная связность, но не текущая торговая неисправность.

**Рекомендация:** при появлении второго adapter/смене storage перенести используемые ошибки в существующий нейтральный domain/ports модуль и переэкспортировать для совместимости при необходимости. Не вводить абстрактный repository hierarchy и не менять все imports сейчас ради формального DIP. Конкретный pure calculator в Analytics usecase не требует Protocol: заменяемого транспорта там нет.

## Производительность: что пока не оптимизировать вслепую

- Details await operations, затем candles: latency приблизительно складывается. Concurrency может помочь, но нельзя запускать синхронные DB sessions/SDK ресурсы одновременно без проверки lifecycle и rate limits. Сначала A1 и замер каждого источника; partial errors должны сохраниться независимо.
- Ingress делает envelope identity/sequence проверку для каждого факта. Это O(F) queries, но здесь guards защищают exact replay и конфликты внутри одного входного batch. Bulk-prefetch без внутригруппового index может пропустить повтор/конфликт. Нужен профиль, не простое удаление guards.
- Analytics уже batch-oriented и не имеет DB; дополнительный кэш по instrument ID без snapshot/profile identity нарушит freshness. Оснований менять boundary сейчас нет.

## Порядок работ и критерии

1. A1: отдельный RED на неправильном ID → минимальное scoped resolution → независимое source/test review.
2. A2: isolated count benchmark; при подтверждении request-local reuse, эквивалентность ответа и измеренное снижение SELECT.
3. A3: воспроизвести interleaving и измерить историю; затем только обоснованный storage snapshot design с сохранением atomic finalization.
4. A4: локальная диагностика, safe response неизменен. A5 оставить точечным долгом до реального adapter изменения.

Новые DDD entity classes для каждого DTO, универсальные UoW/Repository base classes, event bus, distributed transactions и дополнительные сервисные слои не предложены: существующие агрегатные инварианты уже имеют владельцев. Финальный независимый review этого аудита/последующих designs выполняется другим агентом; автор не объявляет собственный аудит кросс-ревью пройденным.
