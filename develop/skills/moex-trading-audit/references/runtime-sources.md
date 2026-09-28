# Источники данных и правила чтения

Пути ниже относительно корня MOEX Sentinel. Сверяй названия и поля с текущим кодом; это карта источников, не зафиксированная версия контрактов.

## API

| Данные | Источник |
|---|---|
| Брокерские позиции (quantity — лоты) | `GET /api/positions`; `adapters/tinvest/portfolio.py`, `views/schemas/portfolio.py` |
| Подтверждённые автоматы Core | `GET /api/trading-automations` |
| Сводка и ошибки сбора | `GET /api/trading/summary` |
| Состояние сессий | `GET /api/trading-sessions/status`; само по себе не доказывает закрытую биржу |
| Карточка инструмента/свечи | `views/market_data.py`: внешний instrument ID |
| Каталог | `GET /api/brokers/{id}/instruments`; возвращает внутренний `id` и внешний `instrument_id`; чтение также запрашивает цены |
| Core market snapshot | `POST http://backend:8000/internal/v1/market/snapshots` |
| Analytics snapshot | `POST http://analytics:8001/internal/v1/analytics/snapshots` |

Два snapshot POST — чтение рыночных данных, не торговые команды. Запрашивай уже проверяемые инструменты ограниченным batch; такой запрос может задействовать подписки/кэш. Core body: `source_id` брокера и уникальные внешние `instrument_ids`. Analytics дополнительно требует `fallback` с `averaging_step_percent`, `minimum_net_profit_percent`, `source`; Worker обычно передаёт источник `STRATEGY`. Порты внутренние: запрос выполняй из контейнера без публикации портов.

`GET /api/trading-automations/{id}/details` содержит automation, operations, candles, errors, но не полный журнал решений. Не вызывай массово брокерские чтения каждую секунду.

## Runtime и журналы

```bash
docker compose ps
docker compose exec -T trading-automaton python -c 'from trading_automaton.config import StrategySettings; print(StrategySettings().model_dump_json())'
```

Логи собирай через `subprocess.run([...], capture_output=True)` в памяти: `docker compose logs --since 15m --tail 10000 --no-color --no-log-prefix trading-automaton`. Не печатай и не сохраняй raw stdout/stderr. Разбери JSON, выводи только allowlist структурных полей (timestamp, level, stage, reason_code, process_id, automation_id, HTTP status); свободные message/URL/exception исключай или редактируй до вывода. Неразобранные строки учитывай счётчиком без их содержимого. При ошибке команды сообщи код возврата, не raw stderr.

`StrategySettings()` здесь читает окружение контейнера; сравни с snapshot сохранённых решений и проверь, что код образа соответствует анализируемому коду. Не выводи целиком `env`, `docker inspect` или broker credentials. Сохраняй safe audit stages, WARNING/ERROR и неуспешные HTTP-ответы, включая записи уровня INFO. Фиксируй ограничения `since/tail`: отсутствие записи в усечённой выборке не означает отсутствие во всей истории.

## Durable журнал

Worker: `src/trading_automaton/storage/models.py`, SQLite-путь берётся из `AutomatonSettings.database_url`.

| Таблица | Назначение |
|---|---|
| `cached_automations` | Core-команда в cache, lifecycle, revision/sequence, lot_size, internal/external ID и bootstrap |
| `trade_decisions` | process_id/time, qty/average/bid/ask, decision/reason, strategy_snapshot, intent_id |
| `business_audit_events` | `STRATEGY_DECISION_MADE`, guards/пороги/cash/cycle, reconciliation и order stages; данные в `event_data` |
| `trade_lots`, `trading_cycle_states` | Лоты и их происхождение, pending_low, sell_armed, last_buy_candle |
| `broker_intents` | Активные/терминальные intent и исполнения |
| `account_commission_profiles` | Ставки как доли, источник и valid_until |
| `fact_outbox` | Доставка фактов: state/retries/revision/sequence; ACK может удалять строку |

Предпочитай согласованную копию SQLite через backup API, особенно для тяжёлого анализа; простое копирование `.db` без WAL не гарантирует согласованность. Короткое диагностическое чтение: sqlite URI `mode=ro`, `PRAGMA query_only=ON`, read transaction, ограниченные SELECT. Не применять миграции или тестовые записи на рабочем экземпляре.

Core: `src/moex_sentinel/storage/models/`. Таблицы `trade_decisions`, `trade_audit_events`, `automation_events`, `trading_automations`, `position_cycles`, `position_lots`. В диагностической транзакции задавай `SET TRANSACTION READ ONLY` и небольшой `statement_timeout`; не выводи DATABASE_URL. Для точной корреляции фильтруй scope broker/automation/process/event, не только ticker.

Для каждой выгрузки фиксируй scope, WHERE, ORDER BY, LIMIT и признак усечения (если неизвестен — так и укажи); отсутствие строки в выборке не доказывает отсутствие события вообще. Время решения Worker — `occurred_at`, Core — `decided_at`.

Core decision содержит `indicators`, которых нет в локальной таблице Worker. Но в текущем `decision_materialization.py` не все поля окна/диапазона/свечи попадают в факт: полного replay может не получиться. Не считай это доказательством неверного BUY/WAIT.

## Где проверять формулы и причины

- `docs/trading-strategy.md`, `trading_automaton/config.py` — спецификация и настройки.
- `services/position_batch_scheduler.py`, `order_book_validation.py`, `analytics_runtime.py` — guards, источник времени, пропуск кадров.
- `services/decision.py`, `strategies.py`, `decision_context.py` — приоритет, стоп/TP, частичная продажа, средняя цена.
- `services/decision_materialization.py`, `streaming_cycle_transition.py`, `trading_cycle.py` — cash, аудит, минимум, cooldown/rearm.
- `market_analytics/indicators.py`, `volatility_strategy.py`, `service.py` — фактические индикаторы, непрерывность, freshness.
- `moex_sentinel/services/market_snapshot_gateway.py` — timestamps/подписки/история Core.
- `services/lot_ledger.py`, `position_bootstrap.py`, `streaming_runtime_coordinator.py` — отличие bootstrap от простого reconciliation.
- `services/fact_synchronization.py`, Worker `storage/repository.py`, Core обработчик trading facts — ACK/reject и согласованность.

Пути без `src/` в последних пунктах начинаются с `src/trading_automaton/`, кроме явно указанных `market_analytics` и `moex_sentinel`.
