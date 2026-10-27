# D3 — scoped operations history

## Реализация

Добавлен authenticated read-only `GET /api/operations/scoped` с точным broker/account scope, обязательным aware `from_at`/`to_at`, `limit` 1–500 и опциональным opaque `cursor` 1–1024 символа. Даты любого offset нормализуются в UTC и миллисекунды; принимается только `(from_at, to_at]`, `from_at < to_at`, верхняя граница не в будущем. Ограничения максимальной длительности и offset `+00:00` не добавлялись.

Сервис делает один broker page read за запрос и возвращает все выбранные записи этой страницы без обрезки по числу отфильтрованных операций. Ответ включает `input_cursor`, исходный `next_cursor`, исходное и выбранное число записей, `source_exhausted` и `coverage_complete`. Полное покрытие отмечается только для первого запроса, когда источник сообщил об исчерпании страницы без ошибок. Выбор другого broker/account, выключенный broker или неверное окружение не приводит к чтению соседнего scope; брокерский текст ошибки/credential в ответ не попадает.

Внутри страницы одинаковые операции с одним ID дедуплицируются, конфликтующие дубликаты, повторный/невалидный курсор, превышение размера страницы, чужой account и невалидные timestamp дают безопасную неполную ошибку. Операции сортируются по времени убыванию и ID возрастанию. Межстраничное состояние не хранится: клиент продолжает чтение переданным cursor и сам обрабатывает повторы между страницами.

Старый `/api/operations`, frontend и `view_position_operations` не менялись. `composition.py`, `api/app.py`, adapter, финансовый snapshot collector, recovery и БД не затрагивались.

## Файлы

- `src/moex_sentinel/domain/portfolio.py` — DTO одной scoped page.
- `src/moex_sentinel/services/portfolio.py` — broker/account ownership, одностраничное чтение, фильтрация, дедупликация, порядок и completeness.
- `src/moex_sentinel/usecases/portfolio.py` — scoped usecase и проверка limit/cursor.
- `src/moex_sentinel/views/portfolio.py` — маршрут и границы времени/query validation.
- `src/moex_sentinel/views/schemas/portfolio.py` — additive scoped response schema.
- `tests/services/test_portfolio_aggregation_service.py` — источник 500 записей, фильтрация, client cursor chain, scope, даты, порядок, ошибки.
- `tests/api/test_portfolio_operations.py` — точный JSON, timezone offset/millisecond normalization, 422 validation, старый маршрут и auth.

SHA-256 на момент отчёта:

```text
f607964288301fae286a4c383f16dc39b929a021921538665bc96a97fef2c498  src/moex_sentinel/domain/portfolio.py
bb194239ca0bd8d9be71558b7c328b5ec28a919e6839607979f416233d6919e5  src/moex_sentinel/services/portfolio.py
e9188e5acac64dcaa627daeee752a7c0458972dc9d3600d7b8e153138578c6b3  src/moex_sentinel/usecases/portfolio.py
87cf8b3708b5a8320d76d3c4fd483aac6d8971da1a962e19d9d9ce7a4306ead8  src/moex_sentinel/views/portfolio.py
9f5e31f6a804540427cb8015664d9c69cf7dddbf0019683eff8594d54e40df6d  src/moex_sentinel/views/schemas/portfolio.py
5caf68ba9dbfa906283e55473d1c036351d3938c5cb68319f4a69b920d52429c  tests/services/test_portfolio_aggregation_service.py
8bbdfda03f08ceef4134066157eb0bc05b6e992e2fe6882daba4fc3b9e281bd8  tests/api/test_portfolio_operations.py
```

## Проверки

RED: `BROKER_ACCESS_MODE=READ_ONLY .venv/bin/pytest -q tests/api/test_portfolio_operations.py tests/services/test_portfolio_aggregation_service.py -k 'scoped_operations or legacy_operations_endpoint'` — scoped тесты падали из-за отсутствующих route/service поведения; legacy endpoint оставался доступен.

Первичная проверка дала 67 passed. После закрытия reviewer M1 gap итоговая D3-focused команда `BROKER_ACCESS_MODE=READ_ONLY .venv/bin/pytest -q tests/api/test_portfolio_operations.py tests/services/test_portfolio_aggregation_service.py tests/services/test_portfolio_contract.py tests/usecases/test_broker_usecases.py` — **55 passed**, exit 0. Отдельная проверка auth boundary `BROKER_ACCESS_MODE=READ_ONLY .venv/bin/pytest -q tests/api/test_auth.py -k anonymous_api` — **1 passed**, exit 0. Добавленные случаи подтверждают обязательные `from_at`/`to_at`, 422 для будущего `to_at` при корректном порядке интервала, отказ disabled broker до создания adapter и sanitized неполный ответ на naive timestamp источника.

`ruff check` только двух тестовых файлов — exit 0. `black --check` только двух тестовых файлов — exit 0. `git diff --check` — exit 0. Использованы только локальные изолированные TestClient SQLite файлы; PostgreSQL, рабочая БД, SSH и runtime не использовались.

Повтор исходного combined command вместе со всем `tests/api/test_auth.py` дал 71 passed и один известный параллельный D1 сбой: `test_two_contours_share_host_but_reject_each_others_origin_and_csrf` сравнивает прежний `/api/runtime` JSON, а рабочее дерево теперь также включает D1 поле `execution_diagnostics: v1`. Это не D3-owned файл и не изменялось в этой работе; root уведомлён. Целевой anonymous auth тест для scoped endpoint прошёл отдельно.

## Приёмка и ограничения

- Acceptance по локальному D3 scope и DoD выполнен.
- Независимый D3 review: GO, C0/I0/M1; источник reviewer — точный scope diff и совпавшие исходные source hashes. Единственный M1 был закрыт добавленными behavior tests. Ожидается scoped re-review этих новых тестов; root ведёт review receipt.
- Полный backend suite оставлен root для интеграционного прогона.
- API не хранит межстраничный state и не обещает глобальную дедупликацию across pages; клиенту следует применять cursor последовательно и дедуплицировать IDs при обнаружении повторов.
- Риск: broker cursor opaque, поэтому после смены токена/счёта источник может отвергнуть его; ответ ошибки остаётся sanitized и не утверждает полноту.
