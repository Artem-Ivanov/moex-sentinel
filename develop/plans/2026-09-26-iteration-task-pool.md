# Состояние вех и пул задач на 26.09.2026 (МСК)

## Что подтверждено

- 0.1–0.8 и 0.9.3–0.9.7 закрыты по
  `docs/phase-0-trading-service-refactor.md` и
  `docs/phase-1-implementation-plan.md`; 0.9.1 поглощена 0.5, а 0.9.2
  условно отложена. Это исторические приёмки, не повторная проверка сервисов.
- 0.9.8 (Worker outbox W1/W2) в работе: W1, W2a и W2b выполнены 26.09;
  durable retry назначается от свежих часов, bootstrap selector ограничивает
  удержание кандидатов. Исходная проблема W2 была полной загрузкой PENDING
  ORM/payload при возможном bootstrap; она устранена W2b. Постановка:
  `docs/milestone-0.9.8-outbox-delivery.md`.
- Исправление Core transient recovery 19.09 находится в рабочем дереве, не в
  HEAD. Сохранённый `runtime/after-deployment.json` подтверждает контейнер с
  совпадающими хешами исходников и healthy на 19.09 09:57 UTC; он не подтверждает
  продвижение решений, доставку или устойчивость после этой минуты. Gateway
  regression 26.09: 44 passed. Полная PostgreSQL-регрессия для этого состояния
  не проводилась (сохранённый прогон: 1176 passed, 55 skipped).
- F1 из старой очереди уже реализована в HEAD: guards после unmount и от
  перекрытия запросов, профильные тесты есть. Повторная задача на её реализацию
  не нужна. Тесты frontend в этой сессии не запущены: `npm` отсутствует.
- Рабочий Docker daemon 26.09 недоступен. Текущий runtime и торговля неизвестны;
  отчёты 18–19.09 нельзя считать текущим состоянием.

## Цель ближайшего закрытия

Закрыть ограниченную recovery-веху доказательством работы после deploy, затем
завершить V0 и принять 0.9.8. Это две отдельные приёмки: восстановление рынка
не доказывает корректность outbox, а тесты outbox не доказывают живую торговлю.
При отсутствии Docker/runtime можно завершить код 0.9.8, но оперативную
recovery-веху оставить открытой с явно указанным препятствием.

## Пакеты для реализации более дешёвыми моделями

Модель означает рекомендуемую минимальную мощность исполнителя; независимый
рецензент обязателен для каждого изменения по `AGENTS.md`. Исполнитель не
подтверждает собственное ревью. Все пакеты сохраняют существующие untracked
файлы, index и volumes. Разовые скрипты и результаты — только в `develop/`.

| ID | Исполнитель | Зависимость | Результат и область | Приёмка |
| --- | --- | --- | --- | --- |
| R0 | `gpt-6-sol`, read-only | доступный Docker/runtime | Свежий bounded снимок Core/Worker/broker после recovery deploy, сверка хешей, шести инструментов, exact `process_id`, outbox/intents и broker/Core/Worker quantities; указать время и статус рынка. Использовать существующие capture-скрипты после проверки их read-only режима. | При валидном рынке показать новые решения, exact доставку и согласование состояний; при закрытом/невалидном рынке или transient отказе оставить recovery открытой и назначить наблюдение после открытия либо адресное исправление. Записать длительность окна и ограничения, не заявлять overnight/SLA. Отдельный независимый рецензент проверяет доказательства. Без скрытых restart и изменений настроек. |
| W1 | `gpt-6-sol` | нет | `src/trading_automaton/services/fact_synchronization.py` и `tests/trading_automaton/services/test_fact_synchronization.py`: тест движущихся часов и минимальное исправление времени назначения durable retry после долгих transport/5xx попыток и retryable group failure. | `next_retry_at = now_at_scheduling + existing_backoff`, до due новый flush не отправляет строки; сохранены selective ACK, retry count/cap и byte-equivalent envelope. Профильные тесты проходят, независимое ревью. |
| W2a | `gpt-6-luna` с ревью `gpt-6-sol` | нет | В `develop/` подготовить синтетические persisted datasets и baseline selector oracle для текущего `ready_fact_outbox`; в `tests/trading_automaton/storage/test_fact_outbox.py` закрепить наблюдаемое поведение. Production-код не менять. | Limits 0/1/2/100, quartet и HOLD, delayed/FAILED/blocked heads, одинаковые timestamps, deadline boundary, 100/10 000/50 000 строк. Записаны seed, состав batch, число запросов и peak memory baseline. Oracle не объявляет старое поведение правильным при выявленном дефекте. |
| W2b | `gpt-6-sol` | W2a | `src/trading_automaton/storage/repository.py` (при необходимости маленький соседний модуль) и профильные тесты: заменить полную материализацию PENDING в bootstrap-ветке ограниченным окном metadata/payload при прежнем порядке отбора. Не менять API и SQLite ownership. | Oracle/new совпадают на допустимых состояниях; `limit + 3` для quartet, blocked prefix, deadline по всем eligible units и selective ACK сохранены; инструментированные пики metadata/payload ограничены размером batch/window при 100/10 000/50 000. Нет накопления страниц в Python/Session. Независимое ревью до интеграции. |
| V0 | `gpt-6-sol` | W1+W2b | Изолированная сквозная проверка Worker SQLite → HTTP → отдельный Core PostgreSQL, benchmark до/после и приёмка 0.9.8. Исходники других подсистем не менять. | Bootstrap, обычный backlog, selective ACK, потеря ACK/restart; конечные sequence/revision и отсутствие потерь/дублированных эффектов. Публиковать p50/p95/p99 selection и drain, CPU/peak memory/SQL на одинаковых данных, включая blocked/FAILED. Полная backend-регрессия без PostgreSQL skips, Ruff/Black и независимое итоговое ревью. |

W1 и W2a независимы и подходят для параллельной работы с разными файлами тестов.
W2b начинается после принятого oracle. V0 начинается после принятия обоих
изменений. Для W2b и V0 нужны более сильные рецензенты: ошибки отбора способны
заблокировать или повторить факты. Если рабочее дерево остаётся общим,
назначить каждому пакету только его файлы и не поручать агентам `git add/commit`.

**Статус 26.09:** W1, W2a и W2b выполнены. W2a независимый рецензент
`/root/review_w2a` и W2b рецензент `/root/review_w2b` завершили с **PASS**.
Frozen compare: 6/6 совпадений на normal/bootstrap 100/10k/50k. Bootstrap
50k old/new: peak tracemalloc 108105444/382335 B, медиана отбора за 3 повтора
6366.750/2894.517 ms, SQL 4/5 ([W2b frozen compare](../reports/0.9.8-w2/selector-comparison-v2.json)).
Чередующиеся normal-медианы old/new: 24.580/25.953 ms при 10k и
65.517/74.404 ms при 50k; общего ускорения не заявляется. При limit=10
детерминированные пики на backlog 100/10k/50k: metadata 24, Session identity
map 10, quartet payload 4, итоговый DTO до limit+3; буфер драйвера отдельно,
до 128 строк. Ограничение относится к удержанию в Python/Session и не убирает
сканирование или затраты буфера драйвера. [W2a baseline](../reports/0.9.8-w2/BASELINE-IN-PROGRESS.md)
— историческая характеристика до W2b; дополнительные normal-замеры — в
[normal perf repeat](../reports/0.9.8-w2/normal-perf-repeat.md).
V0, полный drain, сквозная PostgreSQL-проверка, полная backend-регрессия и
итоговая приёмка 0.9.8 остаются открытыми. R0 также открыт.

## Что удержать вне этой итерации

- Stale CLOSED-status watchdog и ожидание cancellation-resistant drain без
  предела — отдельные сценарии Core reliability из
  `develop/reports/market-recovery-current/code-cause/findings.md`. Их нельзя
  объявить исправленными вместе с transient cooldown. Если R0 выявит один из
  них в текущем инциденте, поднять в срочную отдельную задачу с mock
  воспроизведением, single-owner и freshness регрессией.
- F2/F3, A1, C1–C5 и стратегия/прибыльность остаются в очереди после 0.9.8.
  Для backtest нет полного ряда bid/ask/state/cash; изменение торговых порогов
  не входит в закрытие итерации. F1 убрать из очереди после подтверждения её
  исторической приёмки и доступного frontend test runner.

## Финальный gate координатора

Сверить актуальные HEAD/index/dirty tree, результаты R0 и V0, зафиксировать
независимых рецензентов и устранение существенных замечаний. Затем обновить
`docs/phase-1-implementation-plan.md`, строку 0.9.8 в
`docs/phase-0-trading-service-refactor.md`, приёмку и `develop/WORK_LOG.md`.
Не закрывать 0.9.8 по одним unit-тестам или recovery по сохранённому hash probe;
ограниченное окно R0 не доказывает ночную устойчивость.

## Кросс-ревью плана

`/root/review_iteration_pool` независимо проверил три плановых файла,
соответствие roadmap/0.9.8, исходникам W1/W2/F1 и сохранённым материалам
recovery. После исправления статуса 0.9.2, исторической семантики 0.9.5,
конфликта ID и gate R0 результат **PASSED**, существенных замечаний нет.
Ограничения ревью: runtime, БД и frontend тесты рецензент не запускал;
оперативная recovery и 0.9.8 приёмка остаются открытыми.
