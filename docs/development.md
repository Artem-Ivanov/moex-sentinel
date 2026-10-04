# Разработка

## Версии сервисов

Текущая версия исходного кода backend и frontend — `0.2.0`. Она описывает
подготовленный выпуск; версия установленного на VPS экземпляра проверяется отдельно.
V1 локальная приёмка исходников DONE; итоговая независимая приёмка PASS. Отдельный
deployment receipt записан ниже. [Постановка и задачи](superpowers/plans/v1-service-versions.md).

Последний подтверждённый deploy: commit `be84ed4f047734ccacfba9b6ccb531af51ba4b94`,
release `vps-20261003-be84ed4`, 03.10.2026 17:14 UTC; evidence
`develop/reports/vps-release-20261003/root-native-receipts.json` и отдельный post-deploy
HTTPS verification PASS. Observed Core versions TEST/PROD `0.2.0`, TEST Worker/Analytics
`0.2.0`; PROD Worker UNKNOWN, Analytics NOT_CONFIGURED. VPS schema
`0003_user_broker_archive`; source Alembic head — `0003_user_broker_archive`. UNKNOWN и
наблюдаемая версия не показывают торговую готовность.

Python-сервисы Core, Worker, Analytics и сборщик портфеля выпускаются с общей
версией backend. Согласованно обновляются `project.version` в `pyproject.toml`,
версия локального пакета `moex-sentinel` в `uv.lock` и literal `SERVICE_VERSION`
в `src/sentinel_contracts/version.py`. Совместимый `moex_sentinel.__version__`
импортирует эту константу. Shared version module содержит только docstring и
один literal assignment; Core initializer — shared alias и optional `__all__`.
Повторные вычисляемые присваивания и переопределение alias блокируют выпуск.
Analytics импортирует только shared contracts, без Core/Worker/credentials.

Frontend имеет собственную версию в `frontend/package.json`; обновляются также
корневая версия и `packages[""].version` в `frontend/package-lock.json`.
Версии Python и npm могут различаться; формат каждого пакета — `MAJOR.MINOR.PATCH`.
Схема БД, версии зависимостей и runtime-протокол изменяются отдельно.

На странице «Диагностика» версия интерфейса берётся из собранного пакета и видна
даже при ошибке API. Core сообщает свою версию в health и diagnostics. Worker
передаёт version/instance/контур/режим через существующий heartbeat: Core хранит
один volatile observation, без таблиц и истории; при возрасте >=30 секунд или
после restart Core версия UNKNOWN. Heartbeat без metadata получает прежний
ACK. Scope mismatch, старые/повторные и недопустимые будущие наблюдения не продлевают TTL.
Это информация о версии; она не подтверждает работу стратегии или торговую готовность.

Analytics сообщает версию через private `/health` и OpenAPI. Core использует
captured `ANALYTICS_URL`: TEST Compose задаёт внутренний адрес, initial PROD
явно пустой URL и UNKNOWN. Проверка ограничена одной секундой и телом4KiB,
без broker RPC, credentials, proxy environment и redirects. Ошибки дают UNKNOWN.
В UI Core/Worker/Analytics показаны отдельно; отсутствующие или устаревшие версии
не заменяются версией Core. В локальном O1.2 добавлены цикл управления Worker и
очередь доставки событий: версия, успешный цикл и торговая готовность различаются.
Порог устаревания наблюдения30s; ошибка/заблокированная доставка/нет прогресса
дают DEGRADED, неполученное или устаревшее наблюдение — UNKNOWN. Outbox failed>0
или oldest pending>=60s дают DEGRADED. Broker/market/portfolio/strategy observations
остаются неизвестными. O1.2 ещё не развёрнут; новый Core обновляется раньше Worker.

Проверка перед выпуском:

```bash
PYTHONPATH=src .venv/bin/python -m sentinel_contracts.release .
npm --prefix frontend run build
```

Guard читает metadata через stdlib, не импортируя приложение. Python base Docker
build блокируется своим `--python-only` gate до установки зависимостей; временный
metadata tree и Core alias удаляются до runtime Analytics. Frontend Docker build
выполняет Vite npm gate. Remote preflight проверяет оба пакета до чтения secret env.
Каждый gate блокирует mismatch/missing/malformed metadata; пакеты не принуждаются
к общей версии. При rollout сначала обновляется Core (новый optional heartbeat),
затем Worker; новые Worker metadata требуют поддерживающего Core.

Перед выпуском также выполняются регрессия и независимое кросс-ревью по
[правилам оркестрации](agent-orchestration.md). Владелец делает commit; VPS
обновляется после его отдельной отмашки. [Очередь](phase-1-implementation-plan.md#v1-service-versions).

## Кросс-ревью

Для каждого завершённого изменения обязательно независимое кросс-ревью
саб-агентами по [правилам проекта](../AGENTS.md). Проверяются соответствие
задаче, SOLID, чистота кода, тесты и взаимодействие компонентов. Автор не
утверждает собственную реализацию единолично. Существенные замечания
исправляются и повторно проверяются; результат записывается в отчёт о вехе.

## Вспомогательные инструменты

Одноразовые скрипты, диагностические утилиты и экспериментальный код размещаются
в [develop/](../develop/README.md). Эта папка исключена из Docker-контекста и
Python-дистрибутива. Рабочие модули приложения находятся в `src/`, тесты —
в `tests/`, миграции схемы — в `alembic/`.

## Локальная среда

```bash
cp .env.example .env
uv sync --all-extras
npm --prefix frontend install
uv run python -m moex_sentinel.api.auth hash-password
```

Перед запуском сохраните выведенный verifier в `AUTH_PASSWORD_HASH` файла
`.env` и задайте `AUTH_USERNAME`. Локальный HTTP-вход на `127.0.0.1` требует
явного `AUTH_INSECURE_LOOPBACK=true`; оставьте `false` для HTTPS. Команда Vite
для разработки также слушает только `127.0.0.1`.

```bash
docker volume create moex-sentinel-postgres-data
docker volume create moex-sentinel_automaton-data
docker compose -f compose.yml --profile build build python-base
docker compose -f compose.yml --profile migrations run --build --rm migrations
docker compose -f compose.yml up --build
```

Имена persistent volumes задаются явно через `POSTGRES_VOLUME_NAME` и `AUTOMATON_VOLUME_NAME`. Команда `moex-migrate-schema` применяет Alembic `head`, сейчас `0003_user_broker_archive`, поверх `0001_baseline`; приложение само схему не меняет. Миграция `0002_portfolio_snapshot_runs` проверяет, что старая `portfolio_snapshots` пуста, затем удаляет и создаёт таблицы заново; непустые данные требуют явного отдельного переноса. Миграция `0003_user_broker_archive` добавляет nullable archive metadata к broker records; она не удаляет финансовую историю. Существующие правила старта приложения сохраняются; `0003` не заменяет и не повторяет поведение `0002`. Торговые таблицы и volumes сохраняются.

`portfolio-snapshot-worker` запускается отдельным Compose-сервисом вместе с основным стеком. Он использует только Core PostgreSQL, не зависит от backend и не подключается к SQLite торгового Worker. Интервал задаётся `PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS` и по умолчанию равен `60` секундам. Состояние можно проверить без изменения данных:

```bash
docker compose -f compose.yml ps portfolio-snapshot-worker
docker compose -f compose.yml logs --tail=100 portfolio-snapshot-worker
```

PostgreSQL advisory lock допускает только один сбор одновременно, в том числе
для соседних минутных интервалов. Ошибки отдельных счетов сохраняются вместе
с успешными снимками. Если обнаруженное пополнение или вывод пересекает
время чтения портфеля, неоднозначный снимок пропускается до следующего сбора.
Брокерский API читает портфель и операции раздельно: эта проверка не даёт
гарантии атомарного внешнего снимка или немедленной видимости операций.

Сохранённая сводка доступна через `GET /api/trading/summary`. До накопления полной истории каждый период содержит `complete: false` и фактическое начало в поле `from`; ошибки отдельных счетов возвращаются отдельно и не скрывают успешные валюты.

Период использует последний общий снимок счетов перед запрошенной границей,
либо первый общий снимок при недостатке истории. Поле `from` показывает
реальную базу расчёта: после перерыва в сборе интервал может быть длиннее
номинальных 24 часов, 7 или 30 суток. `complete` означает наличие базы перед
границей, а не непрерывность минутных снимков.

## Границы хранения

`SANDBOX_RETRY_LIMIT` в корневом `.env` ограничивает повторы рыночного источника
Core, загрузки истории отдельного инструмента и подготовки брокерского такта
Worker. Значение по умолчанию `5` означает первую попытку и пять быстрых повторов через
1/3/5/7/9 секунд; `0` отключает быстрые повторы. Постоянная ошибка
авторизации/конфигурации блокирует источник сразу. После исчерпания Core gateway
возобновляется при изменении конфигурации источника либо перезапуске backend;
Worker после исчерпания быстрых повторов временной ошибки продолжает проверять
подготовку раз в 60 секунд. Успешная подготовка сбрасывает счётчик; закрытие
runtime прерывает ожидание. Постоянная ошибка Worker по-прежнему требует
исправления конфигурации и перезапуска runtime/процесса. Периодическая проверка
не обходит сверку позиций, активных заявок, средств и свежести рынка.
Для применения изменённой переменной пересоздать сервисы:

```bash
docker compose up -d --wait backend trading-automaton
docker compose exec -T frontend nginx -s reload
```

Перезагрузка nginx обновляет адрес backend после пересоздания контейнера.

Это ограничение не удаляет intent/outbox и не заменяет отдельные политики
исполнения заявки, доставки фактов в Core и периодического сбора портфеля.
Подробный контракт — в [вехе 0.9.5](milestone-0.9.5-sandbox-resilience.md).

`GET /api/trading-sessions/status` кеширует успешную сводку интерфейса на 60 секунд
и объединяет одновременные обращения. Изменение активных инструментов, их
внешних ID или подключения брокера сбрасывает результат. Ответы с недоступными
инструментами не кешируются. Проверки торговых решений Worker используют
собственные свежие рыночные данные, а не этот кеш интерфейса.

Analytics работает отдельным Compose-сервисом `analytics` на внутреннем порту
8001. Его образ содержит только `market_analytics` и `sentinel_contracts`;
сервису не передаются credentials и volumes. `ANALYTICS_CORE_URL` задаёт адрес
рыночного gateway в Core, `ANALYTICS_URL` — адрес Analytics для Worker.
Core поддерживает stream и обновляет историю свечей раз в минуту; при разрыве
подтверждение торгового статуса и стакана требуется заново. Изменение или отзыв
конфигурации источника проверяется при чтении. Подписки объединяются на время
жизни источника; lease нескольких Worker пока не предусмотрен.

JSON-контракты находятся в `sentinel_contracts.analytics`. Время и ID поколения
не обновляются при повторном чтении кэша. Двухсекундный TTL включителен;
невалидный инструмент исключается отдельно, невалидный транспортный ответ
отклоняется целиком. Неготовые или старые свечи убирают окно покупки, сохраняя
возможность защитных решений по свежему стакану.

Core PostgreSQL содержит `user_brokers`, справочник инструментов, автоматы, циклы позиций, лоты, решения, заявки, исполнения, оценки и аудит. Worker SQLite содержит cache команд, состояние восстановления, активные intent и `fact_outbox`.

Путь данных:

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

Каждая строка с временем использует UTC и точность до миллисекунд. Для однозначного порядка внутри автомата используется `sequence_number`; глобальный целочисленный идентификатор не добавляется, потому что UUID события обеспечивает идентичность, а sequence — порядок.

## Жизненный цикл Core и Worker

Подтверждённое состояние принадлежит Core. Worker предлагает переходы через
typed outbox; локальный переход не является подтверждением принятия Core.
Общая политика находится в `sentinel_contracts.automation_lifecycle`.

| Состояние | Команды пользователя | Предложения Worker |
| --- | --- | --- |
| IN_QUEUE | HOLD, CLOSED | OPENING, IN_WORK, HOLD, CLOSED |
| OPENING | HOLD, CLOSED | IN_WORK, HOLD, CLOSED |
| IN_WORK | HOLD, CLOSED | HOLD, CLOSED |
| HOLD | IN_QUEUE, CLOSED | IN_WORK только при bootstrap |
| CLOSED | нет | нет |

Повтор команды с тем же конечным состоянием не расходует ревизию. Повтор
принятого события возвращает ACK без повторной записи. Новый state-факт с
запрещённым переходом отклоняется целиком с `INVALID_FACT_STATE`.
Сверка cache Worker с авторитетным ответом Core не создаёт новое предложение.
Запись предложения Worker проверяет исходные state/revision/sequence через
DB CAS в одной транзакции с outbox. Устаревшее конкурентное предложение
отклоняется без изменения состояния, лотов и очереди фактов.

`process_id` связывает reconciliation позиции, решение, планирование и отправку
заявки. Восстановление неопределённого intent — отдельный стабильный процесс,
идентификатор которого сохраняется при повторной попытке и перезапуске.
Каждая audit-стадия имеет `occurred_at` и код `stage`. Время означает момент
записи события; групповой decision-аудит не измеряет длительность стадий.

Команда claim используется одним Worker. Разграничение нескольких одновременно
работающих Worker через lease/fencing не входит в текущий контракт.

## Проверка изменений

```bash
ruff check .
black --check .
pytest
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
docker compose config --quiet
```

PostgreSQL integration запускается только с отдельной тестовой БД через `POSTGRES_TEST_DATABASE_URL`. Runtime-проверка переключения находится в [clean-slate-cutover.md](clean-slate-cutover.md).


## Правила тестового setup и прикладных границ

Общие builders располагаются в тематических helper-модулях, а владение engine,
Session и HTTP client — в function-scoped fixtures с гарантированным закрытием.
Начальное торговое состояние задаётся в тесте явно. Длинная последовательность
reopen/replay/ACK остаётся одним сценарием; независимые endpoints и операции SDK
проверяются отдельно. Одинаковые варианты используют именованную параметризацию.

Pydantic DTO изменяются через явный конструктор, когда нужна повторная валидация.
`model_copy(update=...)` не заменяет её. Глобальных подмен `dataclasses.replace`
и `dataclasses.asdict` в pytest нет. Positional constructor compatibility DTO пока
сохранена; длинные production-вызовы используют именованные поля.

При проверке архитектуры оцениваются направление зависимостей, владельцы
транзакций и ресурсов, а также поведение внедрённых зависимостей. Новые тесты
AST/import-графа не добавляются. Разовые скрипты миграции и инвентаризации находятся
в `develop/`, их результаты не являются runtime-тестами архитектуры.

План и свидетельства текущего упрощения:
[план](superpowers/plans/code-and-test-simplification.md),
[код](audits/code-simplification.md),
[тесты](audits/test-simplification.md).
