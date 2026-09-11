# MOEX Sentinel

Локальная sandbox-платформа с раздельными процессами Core, Analytics и Worker. Core владеет PostgreSQL, справочником, автоматами и всеми подтверждёнными торговыми фактами. Analytics рассчитывает рыночные показатели без доступа к портфелю и credentials. Worker владеет только своей SQLite: восстановлением выполнения, активными намерениями и typed outbox.

Исторические данные не переносятся. При чистом старте фактические открытые позиции брокера принимаются как начальное состояние, без вымышленных заявок, исполнений, комиссий или реализованного P&L.

## Запуск

```bash
cp .env.example .env
docker volume create moex-sentinel-postgres-data
docker volume create moex-sentinel_automaton-data
docker compose -f compose.yml --profile build build python-base
docker compose -f compose.yml --profile migrations run --build --rm migrations
docker compose -f compose.yml up --build
```

Core должен иметь текущую ревизию `0002_portfolio_snapshot_runs`; команда миграции применяет всю цепочку от `0001_baseline` до `head`. Проверка API: `GET /api/health`. Compose также запускает независимый `portfolio-snapshot-worker`, который раз в минуту сохраняет согласованные снимки активных брокерских счетов в Core PostgreSQL.

Для существующей БД на `0001_baseline` предусмотрена forward migration без смены volume. Она заменяет только пустую таблицу старых снимков; при наличии строк прерывается без удаления данных. Порядок обновления описан в [runbook](docs/clean-slate-cutover.md#обновление-существующей-бд-до-сводки-торговли).

После запуска создайте broker connection, укажите `account_id`, синхронизируйте инструменты обычным действием UI/API и запустите Worker. Полная безопасная последовательность находится в [runbook чистого переключения](docs/clean-slate-cutover.md).

Страница `/positions` называется «Торговля» и получает сохранённую сводку через `GET /api/trading/summary`. Первый снимок уже показывает стоимость портфеля и свободные средства, но периоды P&L отмечаются как неполные, пока не накопится история за 24 часа, 7 и 30 суток. Backend при чтении сводки к брокеру не обращается.

## Runtime-схема

```text
broker catalog sync
  -> read open positions + active orders
  -> Core automation HOLD/BOOTSTRAPPING
  -> typed command with bootstrap snapshot
  -> Worker reconciled cycle/lot + typed outbox
  -> Core atomic fact group
  -> final state fact IN_WORK
  -> normal runtime decisions
```

Подтверждённое состояние Worker синхронизируется через три typed endpoint Core:

- `POST /internal/automation-commands` — получить команды;
- `POST /internal/automation-statuses` — сверить ревизии и состояния;
- `POST /internal/automation-facts` — атомарно принять группы фактов.

Рыночный путь: постоянный stream в Core → `POST /internal/v1/market/snapshots`
→ отдельный Analytics → `POST /internal/v1/analytics/snapshots` → Worker.
Снимок содержит стакан, до 120 завершённых минутных свечей, показатели,
`snapshot_id`, `captured_at` и `ttl_ms`. Worker получает все инструменты одним
запросом и проверяет свежесть по своим часам. Просроченные данные запрещают
новые заявки; восстановление исполнения и доставка фактов продолжаются.
Analytics не требует отдельного volume или миграции БД.

Стратегия едина для всех автоматов и загружается из переменных окружения при старте Worker. Настроек стратегии в БД и API редактирования нет. Подробности: [торговая стратегия](docs/trading-strategy.md).

## Проверки

```bash
pytest
ruff check .
black --check .
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
docker compose config --quiet
```

Разработка описана в [docs/development.md](docs/development.md).
