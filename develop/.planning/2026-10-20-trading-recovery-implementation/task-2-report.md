# Task 2 — пакет расследования неопределённой заявки

Статус документа: **DONE**. Статус установления live-причины и исправления: **CONDITIONAL / не выполнено**.

## Результат

Подготовлена read-only процедура для D1 Core endpoint и его cached Worker projection, а также D3 cursor-chain истории операций и, при наличии доказанной identity, точечных read-only broker lookups. Она указывает текущие JSON paths для Core/Worker timestamps, active intent, reservations, orders и accepted envelopes; execution fact сверяется цепочкой Worker `fact_execution_id` → Core `executions[].execution_id` → той же row `fact_id` → accepted envelope `fact_id/event_id` с `TRADE_EXECUTION_RECORDED` и совпадающими scope/order bindings. Отдельный submit-attempt record или подтверждение Worker ACK в D1 JSON не обещаются. Первичный D1 запрос не требует заранее известного intent UUID; focused запрос следует после выбора точного `worker_observation.row.intents[].intent_id` с `active=true`. D2 root-cause fix остаётся conditional до correlated runtime evidence.

Путь: `docs/deployment/uncertain-order-investigation.md`.

## Проверка

- Сверены API-границы и модели с `docs/superpowers/specs/2026-10-20-execution-diagnostics-and-history.md`.
- Сверены ограничения и наблюдения с `develop/.planning/2026-10-20-application-audit/findings.md`, `progress.md` и историческим `develop/reports/broker-not-found-20261019/report.md`.
- Сверен request-only recovery контракт с тестом `tests/trading_automaton/adapters/test_tinvest_broker_session.py::test_prod_recovery_looks_up_exact_request_id_without_listing_orders`.
- JSON shape сверен с текущими `ViewExecutionDiagnosticsUsecase`, `ExecutionDiagnosticsSchema`, `OrderFactsRepository.execution_diagnostics()`, `TradingFactsUnitOfWork.execution_diagnostics_scope()`, `WorkerIntentDiagnostics` и `OperationsPageResponseSchema`/scoped D3 endpoint.
- Business execution ID → envelope ID mapping сверен по Worker `_append_execution_graph`/`FactOutboxWriter.append` и Core `TradingFactMapper` (`TradeExecutionDraft.id` из payload `execution_id`, `fact_id` из envelope event ID).
- Учтены замечания независимого review: initial D1 GET без `intent_id`, cached Worker projection не выдаётся за отдельный опрос, добавлены D3 cursor-chain поля/ограничения, поправлены ACK и неоднозначность HTTP 404.
- Разделены серверное `coverage_complete` для единственной исчерпанной initial page и локальный вывод о completeness целой проверенной cursor chain; multi-page ответы API не переписываются и новое поле не добавляется.
- Проверены Markdown code fences, trailing whitespace, relative links и синтаксис JavaScript illustration (без исполнения); API, SDK, SSH, рабочая БД и runtime не вызывались.
- Независимый review выполняет root после передачи пакета; автор не выставляет recovery PASS.

## Ограничения и открытая зависимость

Причина live `NOT_FOUND/UNCERTAIN` не доказана. Сначала требуется D1 deployment на совместимые Core/Worker версии и отдельные свежие read-only срезы; deployment, runtime расследование и бизнес-fix не входят в этот пакет. До этих шагов нет подтверждённого узкого исправления. Документ не разрешает Resume, cancel, ручную очистку резервов, SQL, повторную отправку или изменение owner/lease. Не использовать старый audit как подтверждение текущего runtime.
