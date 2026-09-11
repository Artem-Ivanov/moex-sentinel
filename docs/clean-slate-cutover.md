# Clean-slate cutover

## 1. Остановить создание новых intent

1. Установить `STRATEGY_ENABLED=false`.
2. Пересоздать только Worker.
3. Подтвердить, что heartbeat и supervisor продолжают работать, а новые intent ID не появляются.
4. Дождаться, пока каждая заявка перейдёт в `FILLED`, `CANCELLED`, `REJECTED` или `EXPIRED`.
5. При `UNCERTAIN`, submitted или partially filled состоянии остановить переключение и оставить текущие БД нетронутыми.
6. Только после terminal-check остановить Worker и Core.

## 2. Отсоединить текущие хранилища без удаления

Через read-only команды `docker compose ps`, `docker compose config` и `docker volume inspect` определить Compose project, точные имена Core/Worker volumes и владельцев mount. Не выводить credentials или broker payloads в документацию.

Остановить сервисы и сохранить volumes отсоединёнными. Для чистого запуска выбрать новые явные значения `POSTGRES_VOLUME_NAME` и `AUTOMATON_VOLUME_NAME`.

## 3. Поднять чистое окружение

1. Создать новые volumes.
2. Запустить PostgreSQL.
3. Выполнить `moex-migrate-schema`; ожидаемая текущая ревизия — `0002_portfolio_snapshot_runs` (включает `0001_baseline`).
4. Запустить Core без Worker и проверить `/api/health`.
5. Через обычный UI/API создать broker connection, проверить доступность и указать активный `account_id`.
6. Запустить синхронизацию инструментов.
7. Убедиться, что `portfolio-snapshot-worker` работает независимо от backend и создал первый снимок; проверить `GET /api/trading/summary`.

Ожидаемый поток:

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

## 4. Runtime acceptance

После старта Worker проверить:

- heartbeat и итерации продолжаются;
- количество лотов и средняя цена равны фактической брокерской позиции;
- исторические realized P&L и комиссии равны нулю;
- bootstrap не создал order/execution;
- недостаток свободных средств даёт WAIT без intent;
- допустимая покупка запрашивает `STRATEGY_BUY_ORDER_LOTS`;
- stop-loss, take-profit и partial-profit активны;
- перезапуск не дублирует автомат, цикл, лот и факты.

Старые volumes сохраняются весь acceptance window.

## STOP — требуется отдельное подтверждение

До любого `docker volume rm`, удаления SQLite или иного старого артефакта вне source control нужно показать точные разрешённые targets и получить новое явное подтверждение пользователя. Без такого подтверждения завершить переключение со старыми volumes в сохранённом отсоединённом состоянии.

## Результат проверки 2026-08-14

1. Worker был переведён в `STRATEGY_ENABLED=false`; новые intent не создавались.
2. После обновления broker access штатный запрос по сохранённому broker order ID подтвердил, что единственная запись `UNCERTAIN` фактически имеет терминальный статус `FILLED`. Других активных заявок нет.
3. Старый стек остановлен. Исторические хранилища сохранены без изменения как отсоединённый бэкап:
   - Core: `moex-sentinel-postgres-data`;
   - Worker: `moex-sentinel_automaton-data`.
4. Созданы новые пустые хранилища:
   - Core: `moex-sentinel-baseline-postgres-data`;
   - Worker: `moex-sentinel_baseline-automaton-data`.
5. На новой Core DB применена единственная revision `0001_baseline`. Backend и Frontend собраны из текущего кода, запущены без Worker и проходят health-check.
6. Broker connection создан. При первой реальной синхронизации обнаружено, что API каталога возвращает инструменты с нулевым `min_price_increment`, хотя baseline-схема и торговый контракт требуют положительное значение. Добавлен regression-тест и фильтрация таких записей до persistence; повторная синхронизация сохранила 4275 валидных инструментов со статусом `SUCCESS`.
7. Broker account исправлен через UI. Из стартового портфеля приняты четыре валидные торговые позиции. Балансовая строка `RUB000UTSTOM` исключена как невалидный торговый инструмент и не создала автоматизацию, цикл или лот.
8. Worker запущен на baseline-хранилище. Все четыре принятые автоматизации достигли `IN_WORK`; исправлены повторное применение bootstrap snapshot, восстановление uncertain intent и согласование immutable order facts.
9. Runtime acceptance выявил избыточный опрос портфеля и ответ брокера `RESOURCE_EXHAUSTED`. Реализован минутный in-memory TTL позиций и свободных средств: после старта требуется свежий снимок, reconciliation переиспользует его, а terminal order инвалидирует весь счёт. Retryable broker error сохраняет runtime закрытым для торговых тиков до успешной подготовки; Redis не добавлялся.

## Исправленные регрессии идентификаторов инструмента

Каталог различает внутренний `id` записи и внешний брокерский `instrument_id`. Зафиксированы и исправлены два проявления их смешения:

- карточка инструмента и endpoint отбора теперь получают внутренний `id`, тогда как свечи загружаются по внешнему `instrument_id` из ответа карточки;
- последние операции обогащаются тикером через отдельный scoped-поиск каталога по внешнему `instrument_id`.

Регрессионные тесты используют разные значения внутреннего и внешнего идентификаторов. Это не позволяет случайно вернуть неоднозначный контракт. Неожиданные ошибки каталога больше не скрываются как отсутствующий тикер.

Старые volumes не удалять до завершения runtime acceptance и отдельного явного подтверждения пользователя.

## Сводка торговли после runtime acceptance

### Обновление существующей БД до сводки торговли

Чистое переключение выше не нужно повторять для обновления с `0001_baseline`.
Сохранить текущие значения `POSTGRES_VOLUME_NAME` и `AUTOMATON_VOLUME_NAME`.
Остановить процесс снимков на время изменения его таблиц и применить миграцию
к той же БД:

```bash
docker compose -f compose.yml stop portfolio-snapshot-worker
docker compose -f compose.yml --profile build build python-base
docker compose -f compose.yml --profile migrations build migrations
docker compose -f compose.yml --profile migrations run --rm migrations
docker compose -f compose.yml up -d --build backend frontend portfolio-snapshot-worker
```

Продолжать последней командой только после успешного завершения миграции.
Ожидаемая ревизия — `0002_portfolio_snapshot_runs`; повторный upgrade идемпотентен.
Миграция требует пустой старой таблицы `portfolio_snapshots` и прерывается при
наличии строк. Такая остановка сохраняет исходные таблицы и ревизию; не очищать
строки для обхода проверки. Нужна отдельная миграция с явно заданной семантикой
переноса прежних снимков. Downgrade также разрешён только при пустых таблицах
новых снимков и запусков сбора.

После успешного upgrade проверить `/api/health`, состояние процесса снимков и
`GET /api/trading/summary`. Сам GET к брокеру не обращается. Торговый Worker
не требуется пересоздавать для обновления таблиц сводки.

Страница «Торговля» получает `GET /api/trading/summary` только из Core PostgreSQL. Отдельный асинхронный `portfolio-snapshot-worker` собирает снимки без Celery/Redis и без зависимости от backend или SQLite торгового Worker. После первого запуска проверить стоимость портфеля и свободные средства; P&L за 24 часа, 7 и 30 суток ожидаемо остаётся неполным до накопления соответствующей истории и содержит фактическую дату начала. Ошибка одного счёта не должна скрывать успешно собранные валюты. Детальный дизайн: `docs/superpowers/specs/2026-08-14-trading-summary-design.md`.
