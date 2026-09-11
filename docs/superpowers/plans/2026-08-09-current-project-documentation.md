# Current Project Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Актуализировать постановку и README проекта и создать нормативное описание фактически реализованной торговой стратегии.

**Architecture:** Документы разделяются по ответственности: `AGENT_BRIEF.md` описывает действующую постановку продукта, `README.md` служит точкой входа разработчика, а `docs/trading-strategy.md` фиксирует исполняемый алгоритм `ADAPTIVE_SCALPING` версии `1.0`. Фактическое поведение извлекается из production-сервисов и матрицы `S-01…S-22`; production-код и семантика тестов не изменяются.

**Tech Stack:** Markdown, Python 3.12, pytest, существующий link-check в `tests/test_documentation.py`.

## Global Constraints

- Документировать только фактически реализованное поведение.
- Не изменять production-код и торговый алгоритм.
- Не изменять бизнес-смысл существующих тестов.
- Не добавлять будущие стратегии и улучшения в нормативную спецификацию.
- Не создавать Git-коммиты.
- После каждого самостоятельного этапа запускать релевантную проверку, в финале — полную backend-регрессию.
- Падения классифицировать по правилам вехи 0: A — интерфейс/семантика, B — расхождение DoD, C — дефект реализации.

---

### Task 1: Нормативная спецификация торговой стратегии

**Files:**
- Create: `docs/trading-strategy.md`
- Reference: `src/trading_automaton/services/decision.py`
- Reference: `src/trading_automaton/services/strategies.py`
- Reference: `src/trading_automaton/services/runtime_decision_planner.py`
- Reference: `src/trading_automaton/services/position_decision_evaluator.py`
- Reference: `src/trading_automaton/services/decision_execution.py`
- Reference: `src/trading_automaton/services/trading_cycle.py`
- Reference: `src/trading_automaton/services/market_indicators.py`
- Reference: `tests/trading_automaton/services/test_strategy_selection_matrix.py`

**Interfaces:**
- Consumes: `DecisionContext`, `TradeDecision`, `StrategyValues`, `MarketIndicators`, `TradingCycleState` и фактические решения сервисов.
- Produces: нормативное описание `ADAPTIVE_SCALPING/1.0`, на которое ссылаются `AGENT_BRIEF.md` и `README.md`.

- [ ] **Step 1: Зафиксировать статус и границы спецификации**

  Описать документ как снимок фактического поведения. Указать, что намеренное изменение стратегии требует одновременного обновления спецификации, production-кода и поведенческих тестов.

- [ ] **Step 2: Описать входные данные и market snapshot**

  Зафиксировать стакан с `best_bid/best_ask`, брокерскую позицию, LIFO-лоты, комиссии, free/reserved cash, durable cycle state и последние 120 завершённых минутных свечей двухчасового окна. Перечислить `mean_5`, `mean_20`, `change_10_percent`, диапазон, направление свечи и адаптивные пороги.

- [ ] **Step 3: Описать порядок decision pipeline**

  Зафиксировать порядок `CoreAvailableRule → ActiveIntentRule → StopLossRule → TakeProfitRule → AdaptiveScalpingStrategy`. Отдельно описать основной streaming entrypoint с суточным cached commission profile и legacy `TradingDecisionPlanner`, который повторно рассчитывает actionable-решение после broker estimate; не смешивать эти два фактических пути.

- [ ] **Step 4: Описать формулы и ветви стратегии**

  В явном виде описать первичный вход, reversal confirmation, cooldown, upper-range filter, LIFO partial sell, aggregate partial sell, комиссии обеих сторон, averaging trigger, downtrend filter, budget/cash guards и выбор limit price по первой цене стакана.

- [ ] **Step 5: Перенести матрицу фактических сценариев**

  Сверить и записать `S-01…S-22` с точными `DecisionKind` и `reason_code` из `test_strategy_selection_matrix.py`. Добавить отдельный перечень всех решений и причин, реально возвращаемых pipeline.

- [ ] **Step 6: Описать исполнение и ограничения**

  Зафиксировать создание intent только для `BUY_MORE/SELL_PART/SELL_ALL`, отсутствие нового intent для `WAIT/NO_ACTION`, активный intent как блокирующий guard, поведение `HOLD/CLOSED` и фактические ограничения: одна стратегия, sandbox-only, первый уровень стакана, отсутствие гарантии прибыльности.

- [ ] **Step 7: Добавить трассировку к коду и тестам**

  Добавить относительные ссылки на перечисленные production-файлы, ADR 0006/0007 и тесты стратегии.

- [ ] **Step 8: Проверить документ**

  Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_documentation.py -q`

  Expected: все относительные ссылки существуют; тест проходит.

### Task 2: Актуальная постановка проекта

**Files:**
- Modify: `AGENT_BRIEF.md`
- Reference: `docs/architecture.md`
- Reference: `docs/phase-0-trading-service-refactor.md`
- Reference: `docs/trading-strategy.md`
- Reference: `docs/contracts/openapi.yaml`

**Interfaces:**
- Consumes: консолидированную архитектуру, актуальный OpenAPI и нормативную стратегию из Task 1.
- Produces: действующую постановку проекта без прототипной CLI/SMA20-SMA50 crossover-схемы, но с явным описанием SMA5/SMA20 как фильтров.

- [ ] **Step 1: Заменить историческую область MVP**

  Описать локальное single-user веб-приложение, T-Invest Sandbox, несколько брокерских конфигураций, несколько одновременно отслеживаемых позиций, Core и независимый `trading_automaton`. Явно исключить production/live trading, авторизацию и другие broker adapters из текущей реализации.

- [ ] **Step 2: Описать пользовательские сценарии**

  Зафиксировать страницы аналитики, брокеров, счетов, инструментов, списка автоматизаций и деталей позиции; создание sandbox-счёта, синхронизацию инструментов, создание автомата, `HOLD/RESUME/CLOSE` и просмотр операций/аудита.

- [ ] **Step 3: Описать компоненты и границы**

  Включить `Vue → FastAPI Core → internal contracts → trading_automaton → T-Invest SDK`, направление `View → Usecase → Service → Adapter/Repository`, constructor DI и отсутствие бизнес-валидации во frontend.

- [ ] **Step 4: Зафиксировать данные, lifecycle и безопасность**

  Описать authoritative Core facts, worker durable execution facts/outbox, статусы `IN_QUEUE/IN_WORK/HOLD/CLOSED`, сопровождение уже принятой заявки в `HOLD`, ручной `RESUME`, sandbox-only endpoint и правило «не подтверждено — не торговать».

- [ ] **Step 5: Обновить тестовую и рабочую дисциплину**

  Зафиксировать Pydantic DTO, `Decimal`, типизированные исключения, отсутствие pass-through mapper, правило трёх, service/usecase/integration tests, запрет unit-тестов View и требование полной регрессии с классификацией A/B/C.

- [ ] **Step 6: Зафиксировать приоритет текущей вехи**

  Указать, что активна веха 0: `0.1` выполнена, `0.6` реализована с ручной UI-приёмкой, следующая последовательность — `0.2 → 0.3 → 0.8`; веха 1 начинается после критических задач вехи 0.

- [ ] **Step 7: Проверить отсутствие исторических требований**

  Run: `rg -n 'Typer|moex-sentinel doctor|одним инструментом|веб-интерфейс.*не входит' AGENT_BRIEF.md`

  Expected: совпадений с устаревшей постановкой нет.

### Task 3: README как актуальная точка входа

**Files:**
- Modify: `README.md`
- Reference: `docs/development.md`
- Reference: `docs/architecture.md`
- Reference: `docs/trading-strategy.md`
- Reference: `docs/phase-0-trading-service-refactor.md`

**Interfaces:**
- Consumes: актуальную постановку Task 2 и нормативную стратегию Task 1.
- Produces: краткое руководство запуска и навигацию по документации.

- [ ] **Step 1: Исправить описание текущего состояния**

  Перечислить реализованные frontend/Core/worker возможности, sandbox orders, lifecycle, audit, hot cache и durable worker storage. Удалить утверждение, что заявки и автономная торговля отсутствуют.

- [ ] **Step 2: Добавить схему компонентов и страницы UI**

  Кратко показать поток Vue/Core/worker/SDK и перечислить основные маршруты UI без дублирования полного OpenAPI.

- [ ] **Step 3: Сохранить проверяемые команды запуска**

  Сохранить точные строки `docker compose -f compose.yml up --build`, `pytest` и `npm --prefix frontend test`. Добавить короткий UV-сценарий и ссылку на `docs/development.md`.

- [ ] **Step 4: Обновить ограничения и навигацию**

  Зафиксировать sandbox-only, single-user, отсутствие live trading и добавить ссылки на архитектуру, стратегию, активную веху, ADR и OpenAPI.

- [ ] **Step 5: Проверить README**

  Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_documentation.py -q`

  Expected: обязательные команды присутствуют, ссылки разрешаются, тест проходит.

### Task 4: Консистентность и регрессия

**Files:**
- Verify: `AGENT_BRIEF.md`
- Verify: `README.md`
- Verify: `docs/trading-strategy.md`
- Verify: `docs/architecture.md`
- Verify: `docs/decisions/0005-application-layering-pattern.md`
- Verify: `docs/decisions/0006-trading-automaton-decision-pipeline.md`
- Verify: `docs/decisions/0007-trading-automaton-runtime-decision-coordinator.md`

**Interfaces:**
- Consumes: результаты Tasks 1–3.
- Produces: проверенный непротиворечивый комплект документации.

- [ ] **Step 1: Выполнить проверку терминов и ссылок**

  Run: `rg -n 'торговая логика пока отсутствует|Создание.*заявки.*отсутств' AGENT_BRIEF.md README.md docs/trading-strategy.md`

  Expected: устаревшие утверждения отсутствуют.

- [ ] **Step 2: Выполнить документационные тесты**

  Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_documentation.py -q`

  Expected: PASS.

- [ ] **Step 3: Выполнить полную backend-регрессию**

  Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`

  Expected: PASS; при падениях представить классификацию A/B/C без изменения бизнес-DoD.

- [ ] **Step 4: Проверить рабочее дерево**

  Run: `git diff -- AGENT_BRIEF.md README.md docs/trading-strategy.md docs/superpowers/specs/2026-08-09-current-project-documentation-design.md docs/superpowers/plans/2026-08-09-current-project-documentation.md`

  Expected: изменена только документация в согласованной области; Git-коммит отсутствует.
