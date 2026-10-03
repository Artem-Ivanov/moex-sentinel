# O1.1 — диагностический снимок Core

Статус: локальная кодовая приёмка DONE, deployment NOT_DEPLOYED. Это первая самостоятельная
кодовая веха O1, полная наблюдаемость остаётся в очереди.

## Цель и границы

Оператор открывает защищённую страницу «Диагностика», видит готовность Core,
БД/схемы и режим текущего экземпляра. Готовый Core не подтверждает готовность
торговли: Worker, broker, Analytics, рынок, outbox, portfolio collector и
фактическая стратегия пока не измеряются и явно показаны как UNKNOWN.

Архитектор `/root/o1_architect` подтвердил: readiness/runtime уже реализованы,
heartbeat stateless, диагностического endpoint/экрана нет. Новая возможность
использует существующий readiness; не меняет Worker протокол, хранение, торги,
auth или public health. Нет новых зависимостей, миграций, фонового polling,
broker RPC или доступа к Worker SQLite.

## Контракт

Authenticated `GET /api/diagnostics/status`, `Cache-Control: no-store`.
Диагностический ответ всегда HTTP200, включая обнаруженный отказ Core.
Анонимный запрос401; старый `/api/health` сохраняет200/503 и прежние поля.

```json
{
  "captured_at": "2026-10-03T09:00:00.123Z",
  "status": "UNKNOWN",
  "core": {
    "status": "OK",
    "version": "0.1.0",
    "database": "ok",
    "schema": "compatible",
    "reason": "READY"
  },
  "runtime": {"environment": "TEST", "access_mode": "READ_ONLY"},
  "awaiting_observations": ["worker", "broker", "analytics", "market", "outbox", "portfolio", "strategy"]
}
```

| Проверка | core.status | database | schema | reason | общий status |
| --- | --- | --- | --- | --- | --- |
| БД/схема готовы | OK | ok | compatible | READY | UNKNOWN |
| БД недоступна | DOWN | error | unknown | DATABASE_UNAVAILABLE | DOWN |
| БД доступна, schema probe false/exception | DEGRADED | ok | not_ready | SCHEMA_NOT_READY | DEGRADED |

Общий OK недопустим на этом этапе. Список awaiting_observations фиксированный,
каждый пункт UNKNOWN; нет вымышленных timestamp/count/age. Schema unknown при
DB down означает, что probe не запускался. not_ready не утверждает конкретную
причину несовместимости: существующий readiness скрывает различия false/error.

captured_at — серверное время окончания проверки, UTC до миллисекунд через
sentinel_contracts.time.utc_now_ms; для тестов часы внедряются. Runtime берётся
из Settings, захваченных create_app, а не из заново прочитанного окружения.
Никакие connection URLs, account IDs, секреты или тексты исключений не выдаются.

## Реализация и интерфейс

GetDiagnosticsStatusUsecase принимает существующий CheckReadinessUsecase,
environment/access_mode и часы; единственное место вычисления таблицы выше.
Синхронный FastAPI handler выполняет синхронную проверку вне event loop.
DTO следуют существующим Pydantic/Usecase patterns, дополнительные слои не нужны.

Vue route `/diagnostics`, name diagnostics, существующий auth guard и apiFetch.
Загрузка при mount, кнопка «Обновить», без interval/retry/polling. Время снимка
показано явно; неизвестность не означает остановку сервиса. Режим доступа не
называется фактическим режимом стратегии. При новой загрузке старый снимок
убирается; после неуспеха остаётся безопасная ошибка/повтор, без старого зелёного
статуса. Кнопка disabled во время запроса. AbortSignal и unmount guard не дают
позднему ответу менять экран. Подписи/статусы доступны клавиатуре и screen reader.

## Приёмка

Три строки таблицы, auth/no-store, captured Settings/UTC ms, подавление секретов,
skip schema probe при DB down, отсутствие broker/market вызовов подтверждены
поведенческими тестами. UI покрывает загрузку/обновление/UNKNOWN/отказы/abort,
навигацию и existing unauthorized policy. Изменённые Python-файлы проходят
Ruff/Black; UI — Vitest/typecheck/build. Независимое ревью двух пакетов и
интеграции, предыдущие незакоммиченные изменения сохраняются побайтово.

Полная O1 (heartbeat runtime identity/TTL/застывшая итерация, business audit,
broker/market/outbox/collector observations), O2/O3, S1/M1, длительная и VPS
приёмка этим этапом не закрываются. Деплой — после commit владельца и отмашки.
