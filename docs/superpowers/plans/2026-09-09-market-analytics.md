# Market Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Track task checks below.

**Goal:** Завершить веху 0.5 и исправить двухчасовое окно свечей в деталях инструмента.

**Architecture:** Core market gateway → отдельный Analytics HTTP service → Worker. Данные исполнения остаются в Worker/Core; Analytics получает только рынок.

**Tech Stack:** Python, FastAPI, httpx, Pydantic, существующий T-Invest SDK в Core, PostgreSQL Core, SQLite Worker, Vue/Vitest.

**Spec:** `docs/superpowers/specs/2026-09-09-market-analytics-design.md`.

## Global Constraints

- Сохранить стратегию 0.8, ttl_ms=2000, максимум 120 завершённых минутных свечей и 100 инструментов запроса.
- Не удалять runtime volumes; тестовая БД отдельно. Не читать секреты в вывод.
- Репозиторий без исходного коммита и с пользовательским индексом: выполнять изменения в текущем workspace, не создавать искусственный baseline и не менять индекс.
- Вспомогательные скрипты находятся только в develop; runtime пакет не включает develop.

## Task 1: Contract and Analytics

Files: `src/sentinel_contracts/analytics.py`, `src/market_analytics/`, compatibility imports в Worker, `tests/market_analytics/`, `tests/contracts/test_analytics.py`.

Interface: `snapshot(MarketSnapshotRequest) -> MarketSourceSnapshot`; Analytics принимает `AnalyticsSnapshotRequest` с `fallback: AdaptiveThresholds`, возвращает `AnalyticsSnapshot` с метриками и freshness. Все вызовы async; Core path `/internal/v1/market/snapshots`, Analytics path `/internal/v1/analytics/snapshots`.

- [x] Написать failing tests: strict JSON round-trip, batch вызов один раз, неизменное исходное время, смешанный stale/fresh пакет, отсутствие privileged imports.
- [x] Перенести чистые расчёты без изменения формул и сохранить legacy imports; реализовать app, health и __main__.
- [x] Прогнать `.venv/bin/pytest tests/market_analytics tests/contracts/test_analytics.py tests/trading_automaton/test_market_indicators_service.py` и независимое review.

## Task 2: Core market gateway

Files: `src/moex_sentinel/services/market_snapshot_gateway.py`, `adapters/tinvest/market_stream_source.py`, `adapters/tinvest/streaming.py`, `views/internal_market.py`, `api/app.py`, tests соответствующих сервисов/маршрутов.

Interface: `gateway.snapshot(MarketSnapshotRequest) -> MarketSourceSnapshot`, `gateway.close()`; lazy per-source stream, contracts из Task 1.

- [x] Синтетические failing tests: union subscriptions, reconnect invalidation, single-flight history merge, стабильное поколение снимка.
- [x] Реализовать persistent market-only SDK source, bounded cache, history refresh и lifecycle HTTP app.
- [x] Проверить unit/HTTP tests и независимое review без обращения к runtime брокеру.

## Task 3: Worker integration

Files: `src/trading_automaton/adapters/analytics_client.py`, `services/analytics_runtime.py`, `services/position_state_hydration.py`, `services/broker_tick_preparation.py`, `services/streaming_batch_tick.py`, `composition.py`, `config.py`, `tests/trading_automaton/test_analytics_runtime.py`.

Interface: async client `snapshot(AnalyticsSnapshotRequest) -> AnalyticsSnapshot`; runtime `replace_commands`, `run`, `close`. Runtime передаёт один свежий batch и метрики того же batch в preparation/hydration; execution session остаётся прежней.

- [x] Failing tests: N позиций/один fetch, TTL inclusivity, stale old book, fresh after outage, reconciliation при ошибке, dedup snapshot, expiration during preparation.
- [x] Реализовать HTTP client, заменить production stream/bootstrap на analytics runtime, сделать hydration потребителем готовых метрик.
- [x] Добавить deadline guard перед durable decision batch и dispatch; проверить отсутствие новых intents при истечении времени.
- [x] Прогнать Worker regression и сквозной синтетический HTTP/decision persistence тест.

## Task 4: Candles UI fix

Files: `frontend/src/` instrument detail/candle cache и соответствующие Vitest tests.

- [x] Воспроизвести устаревание двухчасового окна fake timers, проверить cached/error сценарии.
- [x] Исправить обновление минутных свечей, отбрасывание свечей за пределами окна, lifecycle таймера.
- [x] Vitest и production build; независимое review.

## Task 5: Integration and acceptance

Files: `compose.yml`, `docker/analytics.Dockerfile`, `pyproject.toml`, `.env.example`, README, roadmap/brief/acceptance docs.

- [x] Добавить отдельный Analytics сервис без credentials/volumes, адрес в Worker, healthcheck, package entrypoint.
- [x] Полный pytest на отдельном PostgreSQL + develop tests, frontend test/build, Ruff/Black и Compose tests.
- [x] Независимое итоговое review и исправление подтверждённых замечаний.
- [x] Собрать и обновить локальное окружение после graceful stop Worker, сохранить данные, проверить health/freshness/доставку фактов.
- [x] Зафиксировать фактические результаты приёмки; закрыть 0.5 только после проверок.

## Execution ledger

- Начаты Task 1/2/4 независимыми агентами; root выполняет Task 3/5.
- Решение: source_id обозначает конфигурацию рыночного источника в Core; в Analytics не передаются broker connection/account DTO.
- Решение: «двухчасовые свечи» трактуются по существующему контракту как завершённые минутные свечи последних двух часов.
- Дополнение пользователя: проверять Net P&L; подтверждена разная семантика цены в ответе отправки и опроса исполнения. Добавлены исправление SDK converter и проверяемое восстановление сохранённых исполнений в develop.
- По запросу пользователя добавлены обязательные правила независимого SOLID/clean-code кросс-ревью в AGENTS.md и docs/development.md.
- Cross-review Analytics: analytics_boundary_audit; найденная нестабильность метрик на временной границе исправлена analytics_contract_audit.
- Cross-review Worker/UI: analytics_contract_audit; исправлена проверка envelope TTL при bootstrap. Сквозные тесты HTTP/SQLite/Core ingress подтверждают нормальное исполнение, outage recovery и local cancellation после expiry.
- Итоговая регрессия: 937 Python + 48 develop + 74 frontend; Ruff/Black, TypeScript/build прошли.
- Repair применён и проверен: четыре исполнения, Worker 8/Core 11 строк, оба журнала COMPLETE, повторный dry-run без изменений.
- При операционной приёмке обнаружен внешний отказ потока: независимый HTTP/2 показал gRPC 12 / METHOD_NOT_FOUND перед RST8. Около 12:01 UTC поток восстановился без замены SDK, endpoint или credentials; точная причина не установлена.
- Приёмка закрыта по DoD после восстановления решений и подтверждения доставки фактов. Окно восьми замеров: FRESH по всем шести в 6/8, включая последний, решений +17, exit 0. Окно 30 замеров: FRESH по всем шести в 5/30, финально пять FRESH / один STALE, решений +65, exit 1; этот результат не обозначает непрерывную свежесть всех инструментов. TTL 2000 мс сохранён.
- Независимое review закрытия analytics_boundary_audit: APPROVED с ограничениями. Исходные 12 фактов подтверждены за пять секунд, `retry_count=0` во всех трёх замерах; это не исключает повторов внутри вызова доставки. Подробные результаты в docs/milestone-0.5-acceptance-2026-09-09.md.
