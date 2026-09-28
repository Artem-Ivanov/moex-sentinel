# План упрощения тестовой базы — 15 сентября 2026

Статус: анализ до реализации. В этой работе изменены только документ и вспомогательный AST-инвентаризатор в `develop/`; тесты, `src`, frontend и runtime не менялись. План учитывает AGENTS.md и `docs/deployment/refactoring-performance.md`. Цель — понятные сценарии и меньше повторяемого setup, при сохранении проверяемых торговых инвариантов. Уменьшение числа test cases не является критерием успеха.

## 1. База сравнения и определения

Сценарий — исходное состояние, один смысловой триггер/связная последовательность действий и проверяемый результат. Один сценарий может требовать нескольких assert: например, после повторной доставки проверить количество исполнений, комиссии, остаток лотов и ACK. Число assert и длина функции сами по себе не доказывают избыточность.

Повтор setup — одинаковое создание зависимостей/данных, не являющееся проверяемым действием. Вариации сценария — одинаковый поток действий и контракт результата с разными входами; для них использовать `pytest.param(..., id="описание-случая")`. Проверки одинакового бизнес-инварианта на разных границах (pure function, SQLite, PostgreSQL, HTTP) не считаются автоматически дублями.

Метрики получены `develop/scripts/audit_test_structure_20260915.py`; сырой результат — `develop/reports/trading-recovery-2026-09-15/test-structure-inventory.json`:

- 181 Python-файл в `tests/` и `develop/tests/`, включая helpers, `__init__`, conftest и typing; 157 модулей `test_*.py`.
- 773 определения функций/методов `test_*` по AST; это **не** число развёрнутых параметров или унаследованных тестов.
- 33 fixture-функции; 140 декораторов `parametrize`, 139 без явных `ids=`/`pytest.param(id=...)` по синтаксическому анализу. Автоматические pytest IDs могут быть читаемыми; число не означает 139 обязательных переделок.
- 69 import statements из модулей, содержащих сегмент `test_*`; среди них есть намеренно общие acceptance contracts. Это сигнал связанности, не 69 ошибок.
- Найдено 5 групп совпадающих AST-тел нетестовых функций из разных файлов (игнорируются имя/расположение функции; имена переменных и константы сохраняются). Все пять вручную подтверждены как fixture setup; анализ не обнаруживает все близкие копии.
- `pytest --collect-only -q tests develop/tests`: **1198 cases collected**, 4.68 s. Это текущая collection, а не исторический результат `1129 passed` предыдущего recovery: оба числа описывают разные операции и не сравниваются как изменение покрытия. Запуск не выполнял тесты/БД; в collection есть существующие SDK/Starlette deprecation warnings. Лог: `test-collection-baseline.txt` в том же каталоге отчёта.
- Frontend: **19 `*.spec.ts`**, Vitest + happy-dom (`frontend/vitest.config.ts`), scripts `test`, `typecheck`, `build` уже существуют. Vitest в этом аудите не запускался; число frontend cases и browser E2E coverage не измерены.

### Охват подсистем

| Подсистема | Модули `test_*` / AST test functions | Вывод осмотра |
|---|---:|---|
| Core adapters | 8 / 31 | SDK value/context setup; один omnibus mapping-сценарий, отдельные transport/error контракты оставить |
| Core API | 10 / 17 | Повтор create_app/TestClient/usecase wiring; несколько независимых endpoint-проверок слиты |
| contracts + sentinel_contracts | 4 / 18 | Табличные validation/lifecycle cases; сохранять strict validation и точность времени |
| Core domain | 3 / 12 | Небольшие pure cases; enum/offset вариации снабдить смысловыми IDs, fixture обычно не нужна |
| Core services | 15 / 111 | Общие факт/market builders; stateful retry/cancel/peer-isolation последовательности не распиливать по assert |
| Core storage | 18 / 110 | Главный кандидат на устранение повторного engine/session/seed setup |
| Core usecases | 5 / 14 | Простые локальные stubs; не вводить классы/общий слой ради коротких тестов |
| Интеграция SQLite/HTTP | 10 / 32 | Shared real Core+Worker harness; отделить накопленные независимые A3 regressions |
| Интеграция PostgreSQL | 8 / 18 | AST не учитывает унаследованные cases; сохранить migrated schema, concurrency/RETURNING/index cases |
| Market analytics | 2 / 11 | `instrument`, `request`, `Source` используются другими модулями; перенести builders, а не проверяемые сценарии |
| Migrations | 2 / 6 | Два success-варианта URL параметризовать; failure/secrecy и entrypoint отдельно |
| Worker services | 41 / 234 | Крупнейшая подсистема; fixture resources + явные fake-настройки, не мегахарнесс с флагами |
| Worker adapters | 3 / 20 | Event streams/cancellation и broker-status semantic tests оставить отдельными |
| Worker storage | 5 / 40 | `repository`, `filled_buy`, bootstrap/command helpers выходят за пределы файла |
| Worker composition/runtime | 6 / 27 | Reconnect/process lifecycle — связные интеграционные сценарии; setup можно переиспользовать |
| Корневые tests | 9 / 26 | Compose/documentation/config/architecture; различать operational contracts и проверки реализации |
| `tests/typing` | 0 / 0 | Два Python-файла статических контрактов; не удалять как «pytest их не собирает» |
| `develop/tests` | 8 / 46 | Recovery/repair/benchmark тесты; не переносить в production helpers, хранить изоляцию и journal replay |
| Frontend | 19 specs | API 6, components 6, storage 1, views 5 и App 1; повтор router/render/fetch setup |

## 2. Приоритетные кандидаты

Приоритет определяет порядок безопасного изменения, а не важность бизнес-функции.

| Приоритет | Место и символы | Проблема / предлагаемое изменение |
|---|---|---|
| P0: сначала карта сценариев | `tests/integration/test_net_position_facts.py::test_next_tick_keeps_lifo_basis_when_broker_average_differs_after_partial_sale` | 192 строки, 24 assert, lot_size × delayed_side; внутри находятся свежий P&L/precision, stale quote, replay, stale hydrated quantity и позднее BUY/SELL. Вынести одинаковую подготовку двух BUY+LIFO SELL и публикацию в real-ledger fixture. Выделить сценарии «переоценка после LIFO», «старая котировка», «устаревшая гидратация», «late fill после более нового WAIT». Каждый сохраняет несколько связанных assert; precision/replay обязательны, не удалять при делении. |
| P1: DB setup | `tests/storage/test_order_facts_repository.py::database`, `test_trading_analytics_repository.py::database`, `test_trading_facts_support.py::database` | Точное совпадение create in-memory engine/schema/session + `seed_cycle`. Общая function-scoped fixture ресурсов, отдельный явный `seed_cycle` builder. Не превращать каждую фикстуру автоматически в полную торговую историю. |
| P1: DB setup | `tests/storage/test_position_ledger_repository.py::database`, `test_trading_observability_repository.py::database` | Точное совпадение seed BUY/SELL executions. Общий scoped setup, а allocations/remaining lots задавать в конкретном сценарии. |
| P1: DB setup | `tests/storage/test_reference_catalog_repository.py::database`, `test_user_broker_repository.py::database` | Точное совпадение пустого Core engine/session. Fixture с гарантированным dispose через finally; seed только там, где он нужен. |
| P1: dialect resources | SQLite `test_automation_fact_returning.py::automation_engine` / `test_envelope_lookup.py::envelope_engine`; PostgreSQL одноимённые fixtures | Две группы точных дублей. Разделить reusable SQLite resource и migrated-PostgreSQL resource; общие acceptance assertion helpers вынести из `test_*`. PostgreSQL не заменять SQLite параметром с общим create_all. |
| P1: cross-test imports | `tests/storage/test_trading_facts_models.py::{automation_model,seed_cycle,seed_buy_and_sell_executions}`, `tests/domain/test_trading_facts.py::all_fact_drafts`, `tests/contracts/test_trading_facts_contract.py::all_envelopes` | Эти три модуля импортируются соответственно 13/6/8 statements. Перенести factories в `tests/support/` тематическими модулями; существующий `tests/storage/trading_facts_helpers.py` сохранить/расширить по смыслу. Тесты в исходных файлах остаются проверками этих production contracts, не складом данных. |
| P1: ingress scenario reuse | `tests/services/test_trading_fact_ingress.py::{complete_fact_sequence,assert_cancelled_orders_without_broker_ids_replay,assert_cycle_instrument_lineage_rejects_group_atomically}` | PostgreSQL импортирует сценарии из service test. Перенести builder и общие acceptance checks в `tests/support/fact_ingress.py`. SQLite/PG entrypoints остаются отдельными, одинаковые assertions не копируются. |
| P1: Worker resources | `tests/trading_automaton/storage/test_local_repository.py::{repository,filled_buy,baseline_command}`; `tests/integration/test_open_position_bootstrap.py::bootstrap_context` | Helpers импортируют 5 других модулей (первый файл), управление dispose распределено между тестами, иногда обращаются к `repo._factory`. Fixture возвращает явно engine/factory/repo и закрывает их; facts/команды создают обычные функции. `tests/trading_automaton/command_factory.py` уже полезный образец — не дублировать его. |
| P1: hidden compatibility | `tests/conftest.py::{_replace,_asdict}` и `tests/trading_automaton/services/test_batch_runtime_service.py::batch_pair` | Глобальный monkeypatch стандартных `dataclasses.replace/asdict` меняет процесс целиком. Проверить реальные вызовы, заменить PositionalModel usage явными typed builders/конструкторами с revalidation; обычные dataclass stubs оставить dataclasses. **Не** заменять механически на `model_copy(update=...)`: он не гарантирует ту же валидацию, что текущий `model.__class__(**...)`. Удалять shim отдельным последним шагом после совместимого contract test и полного прогона. Async hook не смешивать с этой задачей. |
| P2: независимые HTTP actions | `tests/api/test_trading_fact_ingress_contract.py::test_baseline_routes_delegate_strict_typed_values`; `tests/api/test_market_data.py::test_market_data_http_contracts` | Claim/status/publish и search/instrument/candles — разные триггеры и DTO. Разделить по endpoint contract; fixture создаёт client и явные stubs. Не делать один универсальный параметр с callbacks и разными типами ответов. |
| P2: SDK mapping | `tests/adapters/test_tinvest_order_execution_adapter.py::{test_maps_order_book_estimate_submit_state_and_cancel,run_order_execution_scenario}` | Сейчас проверяется девять независимых адаптерных операций, включая portfolio/status. Оставить общую SDK service/context factory; отдельные book/estimate/submit/status/idempotency lookup/active orders/cancel/position/session cases. Exact-once lifecycle проверяется реальным integration path, а не этим omnibus mock. |
| P2: usecase intent | `tests/usecases/test_broker_usecases.py::test_usecases_reflect_view_save_and_delete_user_intents` | View/save/delete — независимые пользовательские действия; разделить три коротких сценария с одним локальным fixture/factory. Не создавать базовый класс на каждую функцию. |
| P2: параметры | `tests/migrations/test_schema_command.py::{test_schema_command_upgrades_configured_database_to_head,test_schema_command_accepts_percent_encoded_database_url}` | Один успешный путь, меняется URL: `pytest.param(url, id="sqlite")`, `pytest.param(url, id="percent-encoded-postgresql")`. Failure/redaction и backend-no-migration — другие сценарии, не включать в ту же таблицу. |
| P2: parameters | `tests/storage/test_order_facts_repository.py::test_order_update_without_external_id_preserves_known_broker_identity`; `tests/contracts/test_analytics.py::test_request_bounds_and_unique_instrument_ids`; `tests/domain/test_trading_summary.py::test_period_requires_baseline_near_requested_boundary` | Явные IDs для `missing-none`, `missing-empty`, `missing-whitespace`; `empty-request`, `empty-id`, `duplicate-id`, `over-limit`; boundary offsets. Независимые оси не перемножать автоматически, но все важные комбинации из coverage map сохранять. |
| P2: shared market setup | `tests/market_analytics/test_app.py::{instrument,request,Source}`; `tests/trading_automaton/test_analytics_runtime.py`; imports в `test_decision_materialization.py` из `test_position_state_hydration_service.py` | Выделить чистые market/position builders и локальные ресурсы. Fake с mutable streaming state создавать заново на тест; error/cancel режим задавать в тесте, не прятать его в autouse fixture. |
| P2: architecture | `tests/test_architecture.py::test_runtime_tree_contains_no_compatibility_identifiers` | Regex blacklist слов не является проверкой dependency direction; не расширять его именами новых методов. В репозитории уже есть AST-проверка `tests/integration/test_worker_storage_isolation.py::test_typed_worker_fact_paths_do_not_import_core_domain_or_storage`; это существующее ограничение, не образец для новых тестов. ADR 0005 запрещает создавать тесты структуры/AST-графа: направление зависимостей проверяет независимое code review, DI — поведенческие тесты с fake/stub. Обсудить сужение текстового правила без потери согласованных продуктовых запретов. |
| P2: frontend | `PositionDetailsView.spec.ts`, `PositionsListView.spec.ts`, `InstrumentsListView.spec.ts`, `BrokersView.spec.ts` | Повтор createMemoryHistory/router.push/isReady/render, объёмных automation DTO и fetch routing. Общие typed DTO factories и `renderWithRouter` в `frontend/src/test-support/`; response для действия задавать в самом тесте. Cleanup/fake timer restoration — небольшой setup, не скрытый global mock всех API. |
| P3: frontend table | `frontend/src/api/portfolio.spec.ts::loads read-only data from %s` | Таблица уже объединяет один сценарий fetch; первый `%s` форматирует функцию. Добавить смысловое поле name и `$name`, сохраняя URL assertion. Не объединять error/retry/lifecycle DOM сценарии с fetch smoke. |

## 3. Предлагаемые fixtures и места

Это предложение файлов, а не требование создать все сразу. Переносить только реально повторяемое. Следовать `docs/decisions/0004-shared-application-primitives.md`: shared helper требует трёх фактических потребителей и общей семантики либо явно записанного исключения ADR. Один/два использования оставлять локальными по умолчанию. Для двух dialect fixtures допустимое обоснование исключения — единый lifecycle изолированного SQL engine и стабильный acceptance contract, но оно должно попасть в описание конкретной правки. Для двух seed fixtures недостаточно просто совпадения текста: сначала искать третьего существующего потребителя или оставить локальный setup. Число test cases, вызывающих одну fixture, не следует искусственно выдавать за независимые границы переиспользования.

| Ресурс / builder | Место | Scope и ответственность |
|---|---|---|
| `core_engine`, `core_session` | `tests/storage/conftest.py`; при реальном межподсистемном использовании — функции создания в `tests/support/core_database.py` | `function`; yield/finally close/dispose. `seed_cycle` и `seed_buy_sell` явными аргументами/вызовами, не autouse. Не менять commit/rollback semantics тестируемого UoW. |
| `isolated_postgresql_database_url`, `postgres_engine` | существующий `tests/integration/postgresql/conftest.py` | Оставить отдельную тестовую PostgreSQL + уникальную схему + Alembic head + cleanup. `function`, никаких рабочих URL. Migration/DDL tests управляют начальной revision отдельно и не получают уже head без запроса. |
| `worker_repository` | `tests/trading_automaton/conftest.py` + чистая resource factory в `tests/support/worker_database.py` при необходимости integration | `function`; file-backed SQLite через tmp_path для restart/WAL/concurrency, in-memory только для соответствующих tests. Возвращать известные ресурсы, не новый framework/service-класс. |
| `core_worker_ledger` | `tests/integration/conftest.py` с builder функциями `tests/support/ledger_scenarios.py` | `function`; два реальных хранилища + ingress/publish/ACK. Начальное состояние задаёт тест: bootstrap либо две покупки/частичная продажа. Исполнение, которое проверяется, никогда не выполняется скрыто в fixture. |
| `command`, `decision`, `finalization`, `envelope`, `market_book` | расширение существующего command_factory и тематические `tests/support/*` | Чистые функции, explicit keywords, свежие mutable значения на каждый вызов. Различать raw quote timestamp и MillisecondUtc fact timestamp. Не фиксировать все часы целыми секундами. |
| API `client_factory` | локально `tests/api/conftest.py` | `function`; monkeypatch конкретных usecases в пределах теста, TestClient закрывается. Один stub протокола разрешён, иерархия классов на каждый endpoint не нужна. |
| Frontend DTO factories/router renderer | `frontend/src/test-support/` | Новый router/DTO/fetch mock на case; `afterEach` возвращает timers/globals. Сценарные HTTP переходы remain explicit в spec. |
| Repair builders | `develop/tests/` тематический support module | Не импортировать тестовые функции; сохранять реальный SQLite/PG journal + restart поведение. Не уносить разовые recovery данные в общую fixture всех тестов. |

Глобальный/session scope допустим лишь для доказанно immutable констант; mutable sessions, repositories, cash gates, caches, asyncio events, routers и stream fakes нельзя разделять между cases ради скорости. Большой fixture с десятком boolean flags обычно скрывает сценарий — использовать маленькие функции подготовки и явные вызовы.

## 4. Карта сценариев: что обязано сохраниться

| До: существующий путь | После предлагаемого упрощения | Неизменяемые проверки |
|---|---|---|
| `test_net_position_facts` long case: LIFO partial SELL → WAIT | `wait_revalues_remaining_lifo` с fixture двух BUY+SELL; lot_size IDs 1/10/100 | Broker average не подменяет LIFO basis, remaining entry fee вычитается один раз, realized+unrealized→net |
| Там же quote +65 µs | Явный `raw_quote_precision` case/параметр в сценарии end-to-end delivery | Raw DTO время сохранено; typed fact floor до ms; batch не падает; Core.updated_at ожидаемый |
| Там же stale quote и stale quantity | Отдельные `older_quote_is_ignored` и `stale_hydration_uses_durable_lots` | ACK/sequence продвигаются, aggregate не регрессирует, quantity/cost берутся из ledger |
| Там же delayed BUY/SELL после нового WAIT | `delayed_fill_after_valuation` с `pytest.param(side, id="late-buy"/"late-sell-close")` | Ledger quantity/state обновляется в T3, broker execution/closed timestamps T1 сохраняются; replay не дублирует |
| `test_bootstrap_wait_ticks_revalue_and_preserve_original_open_time` | Самостоятельный bootstrap valuation scenario | До исполнения свежий P&L, original opened_at/created_at, отсутствие вымышленных комиссий |
| `test_order_tracking_service` accepted→poll→fill; `test_uncertain_intent_reconciliation_service` | Общий resource setup, разные fault/scenario функции | Исчерпание опроса → UNCERTAIN/HOLD; позднее точное исполнение применяется один раз; cleanup retry не повторяет broker submit |
| `assert_cancelled_orders_without_broker_ids_replay` SQLite/PG | Shared acceptance helper + два dialect entrypoints | Legacy empty ID, два инструмента одного scope, NULL, valid-ID uniqueness, terminal/no fill, sequence и exact replay |
| `test_fact_outbox` head retry/partial ACK/rollback/reopen | Сохранить отдельные сценарии с общей factory | Непрерывные sequence/revision, блокируется только suffix того же automation, атомарность bootstrap quartet, durable reopen |
| `test_automation_fact_returning` / `test_envelope_lookup` SQLite/PG | Shared explicit acceptance contract в support | RETURNING/UTC-ms, no stale identity-map aggregate; PG concurrent one-winner и SQL budget остаются на PG |
| `develop/tests/test_postgresql_benchmark.py`, repair price/journal PG tests | Shared данные, прежние real transport/storage сценарии | Потеря HTTP-ответа после commit → точный replay; restart journal и no duplicate execution; tmpfs не выдаётся за SSD benchmark |
| `test_period_requires_baseline_near_requested_boundary`, summary-service common run | Явные named boundary params + service scenario | Честный from/to/complete, валюты/разрывы/общий baseline не сводятся к одному арифметическому assert |
| API omnibus | Отдельный endpoint contract с общим TestClient | Request typed arguments, response shape/status, ошибки валидации и отсутствие вызова usecase на malformed input |
| Frontend position views | Router/render fixture + case-specific responses | Выбор строки/hold reload, read-only details, обновление и очистка таймеров, price markers при отсутствующей средней |

Не объединять: отмену без broker ID и конфликт реального ID; транспортный retry и permanent error; stale quote и late fill; startup migration и обычный runtime startup; чистую арифметику и HTTP→Core persistence; SQLite и PostgreSQL-specific constraints; cancellation-resistant streaming и обычный teardown. У них различаются триггеры, границы или существенные failure modes.

Связные сценарии вроде «потеря ответа после commit → restart → replay → ровно одно исполнение» должны остаться одним сценарием с несколькими действиями. Не распиливать их на тесты, которым требуется результат предыдущего теста. Не объявлять длинные concurrency tests дублирующими только из-за повторного setup или малочисленных assert.

## 5. Порядок реализации и проверка

1. **T0 — baseline.** Перед первой правкой сохранить collection node IDs, baseline результатов и карту выше; отдельно обозначить skipped PostgreSQL cases. Приоритет пользователя — упрощение тестов, не создание новой архитектуры тестового framework.
2. **T1 — только перемещения.** Если меняются каталоги/файлы тестов, сначала выполнить отдельный шаг с входами, assertions, порядком действий и replay 1:1; новый node ID сопоставить старому. Прогнать исходный и перемещённый целевой набор. Только после этого **T2 — extraction setup** небольшими группами: Core DB → Worker resources → shared factories. Сначала не менять сценарные assertions и expected values. Удаление межтестовых импортов не должно неявно увеличить/уменьшить inherited collection.
3. **T3 — сценарии и параметризация.** Разделить подтверждённые omnibus cases и добавить `pytest.param` с IDs. У нового теста один бизнес-сценарий; количество assert определяется результатом. Параметризовать похожее, не скрывать разные сценарии callback-функциями в таблице.
4. **T4 — критический trading gate.** Для A3 сопоставить каждый старый scenario row с новым node ID, включая +65 µs и delayed BUY/SELL. После деления прогнать оба dialects и review независимым агентом. Не удалять регрессию, воспроизводившую runtime HOLD.
5. **T5 — compatibility/frontend.** Compatibility shim удалять отдельно после явного переноса потребителей и проверки revalidation; frontend helpers также отдельной небольшой группой. Не менять asyncio runner/SDK versions вместе с fixture extraction.
6. **T6 — итоговый gate.** Итоговый cross-review проверяет scenario map, resource cleanup, isolation, причинно-следственные assertions и отсутствие fixture magic. Успех — сценарии читаются, setup имеет одно место, тесты независимы, guards сохранены. Количество cases может увеличиться после разделения.

### Воспроизводимые команды (из корня repo)

```sh
.venv/bin/python develop/scripts/audit_test_structure_20260915.py
.venv/bin/pytest --collect-only -q tests develop/tests
.venv/bin/pytest -q tests/storage tests/services/test_trading_fact_ingress.py
.venv/bin/pytest -q tests/trading_automaton/storage tests/trading_automaton/services/test_batch_runtime_service.py
.venv/bin/pytest -q tests/integration/test_net_position_facts.py tests/integration/test_open_position_bootstrap.py tests/trading_automaton/services/test_order_tracking_service.py tests/trading_automaton/services/test_uncertain_intent_reconciliation_service.py
.venv/bin/pytest -q tests/api tests/adapters tests/contracts tests/sentinel_contracts tests/domain tests/usecases tests/migrations tests/market_analytics
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

PostgreSQL обязателен для финального подтверждения. Сначала поднять **отдельный тестовый экземпляр**, затем передать его URL; переменная не должна указывать на рабочую БД. Ниже `${POSTGRES_TEST_DATABASE_URL:?...}` завершит команду, если явный тестовый URL не задан; сама строка не доказывает, что оператор выбрал правильную БД:

```sh
POSTGRES_TEST_DATABASE_URL="${POSTGRES_TEST_DATABASE_URL:?Set a dedicated disposable PostgreSQL URL}" .venv/bin/pytest -q tests/integration/postgresql develop/tests/test_postgresql_benchmark.py develop/tests/test_repair_execution_prices_postgresql.py
POSTGRES_TEST_DATABASE_URL="${POSTGRES_TEST_DATABASE_URL:?Set a dedicated disposable PostgreSQL URL}" .venv/bin/pytest -q tests develop/tests
```

Первая PostgreSQL команда проверяет dialect-риски, финальная — полный baseline после переносов. Для каждого малого изменения выполнять целевой набор; повторять полный набор после новой существенной интеграции, не после каждой текстовой правки. Timing сравнивать в одинаковом окружении, без заявлений об ускорении по одному запуску collection.

## Ограничения и review

AST и collection выполнены; все перечисленные subsystem категории просмотрены через inventory и конкретные fixtures/scenario examples. Это не формальная проверка каждого assert и не coverage/mutation analysis; runtime performance не измерена. Предложения сохраняют существующие тестовые контракты; недостающие future/stale A5 сценарии — отдельная задача, а не повод незаметно расширять этот refactoring.

Независимый review документа выполнил `/root/audit_review` в составе
[пакета решения](2026-09-15-simplification-plan-review.md). Замечание R1 по
архитектурным проверкам исправлено и повторно проверено; открытых существенных
замечаний нет. Этот документ не разрешает удалять сценарии ради меньшего test count.
