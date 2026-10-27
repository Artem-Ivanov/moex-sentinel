# D1: предварительное независимое ревью

Рецензенты: `audit_review_20261020` (Worker/ingress/cache) и `audit_architect_20261020` (Core facts/API), оба gpt-6.1-sol/high. Авторы код не рецензируют. Вердикт: NO-GO до исправлений и повторной проверки. Пакеты: `review-task-1-worker.md`, `review-task-1-core.md` и соответствующие SHA manifests. Ревью не подтверждает текущее состояние runtime.

## Worker/ingress/cache

- W1 Important: определение active по status не соответствует durable policy. Незавершённые FAILED/терминальные intent могут исчезнуть за пятью новыми finalized rows. Сохранить действующие PROD/TEST инварианты в read-only reader; тесты приоритета и окружений.
- W2 Important: scoped cache не сверяет внутренний fact instrument UUID и lot size. Несовпадение должно давать UNKNOWN без финансовых rows.
- W3 Important: отсутствует durable executed_price. Добавить scalar allowlist поле и пример, где цена исполнения отличается от limit price.
- W4 Minor: handler clear/rollback/close в одном try могут пропустить close. Независимое освобождение и настоящий error/timeout cleanup test.
- W5 Minor: TEST без principal должен сохранять UNAUTHENTICATED_SOURCE, не OWNER_CHANGED.
- W6 Minor: размеры rows/intents ограничивать перед nested validation; проверить oversized malformed payload.
- W7 Minor: optional null должен остаться None, не INVALID_PROJECTION.

## Core

- C1 Important: `session.scalars()` для нескольких reservation columns возвращал строку idempotency key и приводил к HTTP 500. Автор заменил на execute/all и добавил fixture; root focused PostgreSQL проверка ниже прошла. Независимый повторный просмотр ещё нужен.
- C2 Important: общий limit accepted envelopes теряет доказательство приёма видимых event/execution. Возвращать envelopes всех fact IDs из выданных rows, bounded максимумом 3*limit; проверить limit=1 и distinct IDs.
- C3 Important: persisted Core-проекция должна сохранять комиссии, price, decision/process, reservation provenance, scope units; исторические units брать из сохранённого decision, не подменять текущим каталогом. Проверить UNKNOWN Worker и ненулевые суммы/комиссии.

Все замечания переданы D1 автору и входят в текущий scope. Публичный `/api/runtime` должен сохранять старую форму; marker только во внутреннем runtime endpoint.

## Root проверки на промежуточной версии

- Worker/contract/runtime/auth/CoreClient focused: 83 passed, 17 SDK deprecation warnings, 5.09s. `task-1-worker-root-focused.log`. Включён ранее падавший cross-contour auth тест.
- PostgreSQL 16, отдельный tmpfs контейнер, isolated schema fixture: 1 passed, 1.18s. `task-1-postgresql-root.log`. Проверены real reservation, exact acceptance mapping, SELECT без DML/FOR UPDATE. Это не заменяет расширенные сценарии C2/C3.
- Полная итоговая проверка ещё не выполнена; исходники продолжают меняться.
