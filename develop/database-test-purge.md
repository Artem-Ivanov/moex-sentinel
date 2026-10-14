# Разовое удаление TEST из общей Core PostgreSQL

Этот helper выполняет отдельное прямое разрешение пользователя 11.10.2026 на удаление **всех TEST данных**, включая финансовую историю TEST. Месячная retention PROD не меняется. PROD, исходные Docker volumes и остановленная Worker SQLite сохраняются. Удаление финансовой TEST истории необратимо; автоматического восстановления или повторного включения TEST нет.

Оператор — root агент. Helper `develop/scripts/purge_test_data.py` использует только SQLAlchemy и переменную окружения `DATABASE_URL` внутри существующего Core окружения. URL не передавать в аргументах и не печатать. Helper не выполняет SSH, Docker, SDK, deploy, ARM или сделки. Новые зависимости не нужны.

## Обязательный operational fence

До `--apply` подтвердить актуально: TEST Core/Worker/Analytics/frontend остановлены и не перезапускаются; отсутствуют другие TEST producers, collectors, cron, ручные импорты и незавершённые транзакции этих writers. Общий portfolio collector остановить на время операции, дождаться завершения его транзакций; shared PostgreSQL не останавливать. Не менять broker scope/environment/state, portfolio runs, схему/DDL во время purge. Не запускать восстановление TEST outbox или повторную отправку исторических TEST facts после purge.

PROD writers могут продолжать писать в свои scoped таблицы. `--writers-fenced` — явное утверждение оператора об этих выполненных условиях, а не автоматическая проверка процессов. Helper не может доказать остановку удалённых writers. Сам `DISABLED` не заменяет остановку Worker/collector: существующие токены и in-flight операции должны быть закрыты снаружи.

Общий portfolio collector можно вернуть после завершения global portfolio phase,
если TEST scopes уже DISABLED, их приложения остаются остановленными, а фактический
код collector при каждом цикле заново читает scopes и пропускает DISABLED. Root
должен подтвердить это по текущему коду контейнера и отсутствию незавершённой
TEST-транзакции. Это условие проверено отдельно для продолжения 11.10; оно не
разрешает возвращать другие TEST writers.

При apply helper сначала устанавливает `DISABLED` только выбранным TEST scopes. Перед каждым пакетом он блокирует их строки, сверяет TEST/DISABLED и исходное множество scopes. Изменение scope приводит к `SCOPE_DRIFT` без удаления строк нового PROD scope. Защищённая PROD ссылка на TEST runtime и несовместимый runtime.environment приводят к `PROTECTED_REFERENCE` до первого изменения. После сбоя оставшиеся TEST scopes остаются DISABLED; fence сохранять до успешного окончания или отдельного решения оператора.

## Запуск

Из существующего Python окружения с безопасно установленным `DATABASE_URL`:

```sh
python develop/scripts/purge_test_data.py
python develop/scripts/purge_test_data.py --apply --writers-fenced --batch-size 2000
```

Первый запуск — только dry-run. Root сначала сохраняет aggregate receipt, завершает независимое review и проверку на отдельной PostgreSQL, затем выполняет apply. Запуски в контейнере или через stdin организует root отдельно; файл не требует исходников приложения и не зависит от ORM head. Не печатать env или команду с URL. Receipt содержит только режим, фиксированные имена таблиц, candidate/deleted counts и число удалённых TEST error entries; прогресс — каждые 100 пакетов. Исключения БД заменяются стабильным `DATABASE_OPERATION_FAILED`; произвольные ошибки CLI — `PURGE_FAILED`, без SQL/params/URL/settings/account IDs/payload.

## Область удаления и защита

Все 24 таблицы каталога 11.10 классифицированы явно: `alembic_version` сохраняется; 22 scoped таблицы, включая `user_brokers`, удаляются только по точному `environment = 'TEST'` владельца; global `portfolio_snapshot_runs` обрабатывается отдельно. `PRODUCTION`, `NULL` и другие значения не считаются TEST. `worker_runtime_instances` дополнительно требует собственное environment TEST. Новый table/column/PK/FK означает `UNSUPPORTED_SCHEMA` и отказ. Изменения CHECK/index допускаются; migration revision не используется как доказательство совместимости.

Таблицы удаляются по реальному графу 45 FK, от дочерних к родительским. Ограничения, индексы и триггеры не отключаются, schema/cascade DDL не используется. Поиск пакетов использует PK keyset `> cursor`, а не OFFSET или повторное чтение удалённого префикса. Размер пакета — 1…10000 строк. PostgreSQL statement timeout 60 секунд, lock timeout 5 секунд, idle-in-transaction timeout 60 секунд. Dry-run выполняется в READ ONLY транзакции; FK guards не являются полной проверкой целостности рабочей базы.

Portfolio error принадлежит TEST только если его `broker_id` точно равен ID TEST scope (актуальный `BrokerReadError` contract). Unknown/neutral errors сохраняются. TEST snapshots удаляются пакетами; последний пакет и удаление доказанно TEST-only run/TEST errors коммитятся вместе. Поэтому restart не теряет доказательство принадлежности run TEST. Shared/PROD runs сохраняют остальные поля и non-TEST errors. Пустые neutral global anchors сохраняются. Run удаляется только при отсутствии любых оставшихся snapshots; блокировка run защищает от параллельной вставки дочерних snapshots.

## Повтор, проверка и ограничения

При сбое остановиться с сохранённым fence, изучить только безопасный error code, устранить причину и повторить dry-run/apply. Коммит завершённых пакетов не откатывается. Второй успешный apply — no-op. Отдельного permanent progress ledger нет: оставшиеся строки и атомарное завершение run служат продолжением; cursor живёт только внутри вызова.

После операции root сохраняет receipt, возвращает общий collector в прежнее состояние и проверяет health/ping/basic UI. TEST приложения не включать. Не выполнять автоматические VACUUM FULL, backup/restore, удаление volumes или перепроверку всего финансового графа рабочей базы. DELETE освобождает место для повторного использования PostgreSQL, но не обещает немедленное уменьшение файлов на диске.

Тесты: `PYTHONPATH=src:. develop/reports/prod-admission-20261009/.venv-native-actual/bin/python develop/reports/prod-admission-20261009/run_tests.py develop/tests/test_purge_test_data.py -q`. Runner читает секреты только отдельного локального тестового PostgreSQL. Без runner PostgreSQL cases пропускаются; это не доказательство проверки PostgreSQL. Native environment содержит старый installed snapshot, поэтому `PYTHONPATH=src:.` обязателен.

На captured head5 отсутствует индекс `portfolio_snapshots.run_id`. Dry-run использует один GROUP BY проход по snapshots; последняя проверка NOT EXISTS и FK при удалении каждого run всё ещё могут сканировать оставшиеся snapshots. При фактических ~24 тысячах snapshots и ~13 тысячах runs это ограниченный объём; перед значительно большим purge сначала оценить run_id индекс на отдельной БД. Helper сам DDL/index не создаёт. Root следит за aggregate progress; statement timeout действует на отдельный SQL statement, не на всё время purge. Размер данных счётчиков dry-run ограничен количеством runs, строки payload не загружаются в отчёт. Неизвестные пользовательские триггеры, PostgreSQL RLS и входящие FK из других schemas также вызывают отказ.

При продолжении 11.10 проверка FK после удаления TEST audit сканировала оставшийся
большой heap и достигла statement timeout. Отдельный проверенный helper
`scripts/purge_scope_index.py` создаёт временный Btree
`trade_audit_events(user_broker_id)` через CREATE INDEX CONCURRENTLY. По умолчанию
он показывает только отчёт; `--apply` создаёт индекс. Нужны отсутствие активного
purge, независимое ревью и сохранённый receipt `present_before=false, changed=true`.
После завершения purge `--apply --drop` удаляет только этот индекс через DROP INDEX
CONCURRENTLY с проверкой его точного определения. Совпадающий прежний индекс не
считается созданным этой операцией и не даёт права на его удаление. Несовместимое
определение или невалидный существующий индекс останавливают создание; невалидный
индекс можно удалить только после подтверждения принадлежности текущей попытке.
Оригинальный purge helper, ограничения, прочие индексы и финансовые PROD-строки
при этом не меняются.
