# Наблюдаемость MOEX Sentinel

Статус: проект для согласования от 02.10.2026, код/stack не развёрнуты.
03.10 принят отдельный локальный кодовый этап [O1.1](../superpowers/specs/o1-core-diagnostics-design.md):
готовность Core и режим экземпляра, остальные подсистемы явно UNKNOWN.
Задачи и результат приёмки записаны в [плане](../superpowers/plans/o1-core-diagnostics.md).
Этот этап не подтверждает heartbeat, audit query, метрики, alerts или deployment.
V1 добавляет отдельные version observations в diagnostics и собственную UI версию
([план](../superpowers/plans/v1-service-versions.md)); локально DONE / NOT_DEPLOYED,
итоговая независимая приёмка PASS. Version heartbeat имеет volatile TTL и scope, но не измеряет прогресс
итераций, broker/market/outbox или готовность стратегии. Private Analytics health
сообщает версию, не свежесть рынка. Полные O1/O2/S1/M1 остаются OPEN.
Цель — видеть состояние решения и разбирать инциденты через интерфейс,
получать уведомления об отказах без ручного подключения по SSH.

## Что уже есть

Последняя проверка VPS 02.10 подтверждала Docker Compose контейнеры. В коде есть
JSON-логи с UTC/process_id/stage, durable business audit Worker/Core,
описания решений/позиций и периодические portfolio snapshots.
`/api/health` показывает готовность Core DB/schema; Analytics health
не проверяет свежесть рынка. На VPS baseline02.10 heartbeat каждые3секунды
оставался stateless. Локальный V1 сохраняет только version observation, время
приёма Core и TTL/age и возвращает их в diagnostics; прогресс итераций и
broker/outbox/market состояние этим не измеряются. Полноценного HTTP query
журнала аудита, `/metrics`, dashboards и alerting нет.

Зелёный HTTP health поэтому может сосуществовать с остановленным Worker,
недоступным брокером или stale рынком. Это пробел видимости, который нужно
закрыть перед PROD торговлей.

## Варианты

| Вариант | Результат | Ограничение |
|---|---|---|
| Только страница состояния приложения | Быстро видно Worker/брокера/очередь и причину WAIT/HOLD | Нет независимых уведомлений при отказе всего VPS |
| Страница + Prometheus/Grafana и оповещения | Прикладная диагностика, история метрик и графики, alert routing | Дополнительные процессы и bounded хранилище; сначала измерить ресурсы |
| Дополнительно Loki и сборщик логов | Поиск технических событий между сервисами | Дополнительный объём/нагрузка; вводить после замера логов |

Предлагается второй вариант по этапам. Loki и распределённые traces
пока отложены до подтверждённой потребности. Собственный business audit
и `process_id` используем для цепочки решений, а не заменяем её stdout логами.
Baseline02.10 VPS — 1,92GiB RAM, available~0,79GiB в одном снимке: размещение
Prometheus/Grafana вместе с двумя приложениями требует пикового resource gate.
Сначала O1; если resource gate не пройден, согласовать отдельное размещение
мониторинга или увеличение ресурсов, не допускать OOM торговых процессов.

## O1. Статус и журнал в приложении

**API:** новые authenticated GET `/api/diagnostics/status` и
`/api/diagnostics/audit`. Подробный health не публикуется анонимно.

Status возвращает capture time/age и состояния `OK/DEGRADED/UNKNOWN/DOWN`:

- TEST/PROD, masked account и READ_ONLY/TRADE, actual strategy mode;
- последний heartbeat Worker, последняя завершённая итерация и безопасная
  причина ошибки; runtime/generation identity и время приёма Core связывают
  heartbeat с конкретным запуском. Свежий heartbeat при застывшем цикле —
  DEGRADED, после рестарта Core — UNKNOWN до свежего подтверждения;
- последнее успешное broker RPC, latency и safe gRPC error code;
- свежесть Analytics/стакана отдельно от расписания рынка;
- pending/failed outbox count, возраст старейшего и delivery retry;
- UNCERTAIN/active intent counts, устойчивое расхождение Core/Worker/broker;
- время последнего успешного portfolio run и причина частичного/полного отказа;
- состояние operator stop/lease/лимитов, когда эти контракты будут добавлены.

Worker читает собственную SQLite и передаёт Core только ограниченные
диагностические агрегаты через внутренний heartbeat контракт.
Core сохраняет последнее наблюдение с временем приёма/TTL; перезапуск не
может выдавать старое состояние за свежее. В UI неизвестность видна явно.
Core читает свою PostgreSQL; Analytics не получает account/credentials.
Не добавлять SQL доступ Worker к Core или доступ Grafana к broker settings.

Audit query: фильтры времени, process_id, automation/instrument UID,
level/stage/reason; cursor pagination, limit по умолчанию50, максимум200,
safe allowlist полей без token/cookies/сырого SDK error body.
Финансовые значения доступны только оператору соответствующего экземпляра.
Технические diagnostic events и authoritative trade facts различаются.

**Files:** Core `services/automaton_sync.py`, internal/public View и schemas,
`src/sentinel_contracts/` heartbeat/diagnostics DTO, Worker runtime coordinator
и SQLite repository, Core audit repository/query, новая Vue страница состояния.
Текущий heartbeat DTO расширяется согласованно для обоих сервисов.

**Приёмка:**

- [ ] RED/GREEN: Worker остановлен при healthy Core → stale/UNKNOWN;
  последнее наблюдение не «оживает» после restart Core.
- [ ] RED/GREEN: heartbeat продолжает поступать, iteration generation/time
  не продвигаются → DEGRADED; новый запуск не смешивается со старым runtime.
- [ ] RED/GREEN: старый heartbeat/future timestamp/неверный scope не
  заменяют свежее валидное наблюдение; время UTC и age берутся по server clock.
- [ ] RED/GREEN: Core не читает Worker DB; агрегат не содержит секретов;
  public diagnostics/audit требуют auth, audit pagination ограничена.
- [ ] RED/GREEN: broker outage, stuck outbox, stale market и закрытый рынок
  показывают разные причины. Empty/partial/failed snapshot видны отдельно.
- [ ] Проверить интерфейс и query cost на отдельной тестовой БД;
  измерить overhead heartbeat/метрик; независимое ревью до деплоя.

## O2. Метрики, графики и уведомления

Prometheus собирает метрики с private endpoints сервисов, Grafana показывает
их историю и отправляет alerts. Одного механизма alert routing достаточно;
отдельный Alertmanager не добавлять, если выбран Grafana Alerting.
Metrics остаются в Docker internal network; Grafana первоначально доступна
администратору через SSH tunnel/закрытый вход с собственным auth. Открывать
новый публичный dashboard без согласованного доступа не требуется.

Минимальные ряды: HTTP/RPC errors и durations, Worker heartbeat/iteration age,
broker last-success, market snapshot age, outbox count/oldest age/retries,
UNCERTAIN, reconciliation mismatch, portfolio run age, disk available,
container restart/OOM, backup age/status и TLS expiry.
Labels — service/environment/result/reason; account ID, process/event/order ID
не используются как labels из-за чувствительности и высокой cardinality.
Все экспортеры соблюдают границы владельцев хранилищ.

Начальные пороги — **предложение**, калибруются по O1/runtime:

| Сигнал | Начальное правило | Действие |
|---|---|---|
| Worker heartbeat | нет свежего >30с, ещё30с подтверждения | уведомить, запрет новых PROD writes при отсутствии допуска |
| Outbox | oldest >60с устойчиво60с или FAILED>0 | уведомить, показать причину доставки |
| Broker auth/permission | первый подтверждённый отказ | запрет новых writes, оператор проверяет токен |
| UNCERTAIN/mismatch | возникновение/подтверждённое устойчивое расхождение | HOLD/инцидент; автоматическую повторную заявку не создавать |
| Market TTL | истёк кодовый TTL при ожидаемой доступности торгов | блокировать dispatch; при закрытом рынке показать CLOSED |
| Portfolio snapshot | успешного run нет >3 интервалов | уведомить; stale суммы не выдавать за текущие |
| Диск | available <20% warning, <10% critical | уведомить; проверить абсолютный запас и прогноз времени до заполнения |
| Backup/TLS | backup старше выбранного расписания; TLS <3дней | уведомить с конкретной датой/age |

Guard стратегии использует собственные freshness/TTL проверки; мониторинг
и доставка alert не являются единственным механизмом запрета торговых действий.
Закрытая биржа не отменяет heartbeat/доставку фактов и не скрывает отказ брокера.
Для полного отказа VPS нужен **внешний** uptime probe: локальная Grafana тогда
не сможет прислать сообщение. Канал/получатель и внешний probe выбираются
с владельцем; внешние сообщения сейчас не отправляются.

**Приёмка:**

- [ ] Развёртывание с явными memory/CPU/storage budgets после S1 замеров;
  initial metrics retention15дней и size cap по доступному диску.
- [ ] Проверить cardinality, отсутствие секретов и private bind/ports.
- [ ] На отдельном тестовом контуре воспроизвести Worker/Core/broker outage,
  UNKNOWN, заполнение очереди и low disk; увидеть alert и recovery/resolved.
- [ ] Владелец принимает канал; отправить согласованное тестовое уведомление.
  Внешний probe проверить отдельным способом без остановки реальной торговли.
- [ ] Независимое ревью dashboard/rules/retention, overhead и доступа.

## O3. Технические логи и хранение

Remote Compose уже задаёт Docker json-file `10m × 5` на контейнер.
Это ограничение размера, а не обещание хранения N дней.
Nginx/system logs/backup/images в этот лимит не входят.
План замеров и оптимизации — [storage-capacity.md](storage-capacity.md).
Если поиск stdout по сервису/time/process_id всё ещё нужен после O1/O2,
добавить Loki с подходящим сборщиком, приватным доступом и лимитом retention
после измерения скорости логов. Не размножать полные financial payloads
в logs, PostgreSQL audit и Loki без необходимости.

## Источники

- [Prometheus и метрики](https://prometheus.io/docs/tutorials/getting_started/).
- [Grafana Alerting](https://grafana.com/docs/grafana/latest/alerting/).
- [Docker JSON logging и ротация](https://docs.docker.com/engine/logging/drivers/json-file/).

Дальнейшая детализация O1/O2 — отдельные планы реализации после принятия
проекта. Этот документ не утверждает, что мониторинг уже работает на VPS.
