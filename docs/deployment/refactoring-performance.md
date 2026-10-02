# План улучшений перед эксплуатацией на удалённом сервере

Это приоритетный backlog по коду и аудиту 15 сентября 2026, а не заявление об измеренном ускорении. Реализация этих изменений не входит в deployment overlay. Сначала стабилизировать торговые инварианты и провести перенос с подтверждёнными backup/restore; производительность измерять только на отдельной тестовой БД и синтетическом брокере.

Текущий способ развёртывания согласован: SSH → клонирование проверенного Git commit → ручная сборка и запуск по `remote-compose.md`. Предложения по CI ниже относятся к будущему улучшению проверок; автоматизация доставки не требуется для текущего ручного деплоя.

Дополнение по читаемости и сокращению избыточной реализации:
[план упрощения кода и тестов](../superpowers/plans/code-and-test-simplification.md).
Он отдельно разбирает фикстуры, сценарии и параметризацию, docstrings и границы
юзкейсов. Его реализация и независимое ревью завершены; последующие измеряемые
оптимизации outbox/LIFO описаны в обновлении ниже.

### Обновление 16 сентября 2026

После архитектурного аудита реализованы ограниченная SQL-выборка обычного outbox,
чтение колонок LIFO в decision-пути, индекс Worker по intent_id и устранение
повторных catalog lookup внутри запроса операций. Исправлено разрешение ID
инструмента для операций позиции. Измерения, ограничения и статус проверки —
в [итоговом отчёте](../audits/architecture-performance-summary.md).

Таблица ниже сохраняет исходные наблюдения. Пункты outbox/LIFO теперь требуют
проверки на целевом сервере и дальнейшего измерения: bootstrap-очередь сохраняет
прежнюю группировку, LIFO по-прежнему читает всю историю. Полный отказ от O(N),
новая схема агрегатов и увеличение числа процессов этим изменением не заявляются.

| Приоритет | Наблюдение и место | Предлагаемое изменение | Проверка результата |
|---|---|---|---|
| P0 | Аудит: внешняя заявка UNCERTAIN и расхождение Broker/Core/Worker; отсутствующий ID блокировал outbox; WAIT не доставлял P&L. `develop/reports/trading-audit-2026-09-15/tasks.md`, A1–A3. | Закрыть recovery-код и независимое ревью, подтверждение exact-once executions/комиссий и восстановление очереди до переноса. Включить delayed BUY/SELL, потерю ответа после commit и сохранённые старые payload в обязательный release gate. | Broker/Core/Worker quantities сходятся; повтор не создаёт заявки/исполнения; возраст pending очереди ограничен и sequence продвигается. Это критерий корректности, не скорость. |
| P0 перед публичным доступом | `docker/nginx.conf` проксирует весь `/api/`; приложение не имеет auth. | Текущий remote режим — loopback + SSH. Для публичного домена отдельно согласовать auth перед UI/API, TLS и ограничения доступа. Не расширять наружный доступ одним изменением bind-address. | Неавторизованный клиент не читает credentials/позиции и не меняет торговлю; внутренние endpoints недоступны снаружи. |
| P1 | `docker/python-base.Dockerfile`: диапазоны версий pip; дочерние Dockerfiles используют `FROM moex-sentinel-python-base:local`; upstream images заданы mutable tags. `.github` отсутствует. | Зафиксировать совместимые зависимости и digest базовых образов, параметризовать имя base image, выдавать immutable release images с manifest/commit. Добавить CI: backend/frontend проверки, изолированная PostgreSQL, compose preflight и cross-review gate. Сначала сохранить текущую сборку для отката. | Чистые сборки одного lock/commit разрешают одинаковые версии; каждый deployment имеет manifest; предыдущий release запускается без rebuild. |
| P1 | `LocalAutomationRepository.ready_fact_outbox` читает **все** PENDING через `.all()` до применения batch limit; `_fact_publication_units` снова читает bootstrap automations. При backlog после сбоя стоимость растёт с полной очередью. | Замерить backlog 100/10k/100k фактов; затем выбирать ограниченные последовательные группы SQL/keyset с сохранением порядка каждого automation и атомарного bootstrap quartet. Не вводить глобальный LIMIT, разрывающий группу. | p95 drain, RSS, число выбранных SQL rows ограничены размером окна; retry одного scope не блокирует другие; bootstrap/sequence/replay тесты прежние. |
| P1 | После A3 каждый WAIT вызывает `_authoritative_position_snapshot`: загружаются все TradeLot и LotAllocation automation; затем `_append_cycle_fact` ищет opened_at. Это усиливает нагрузку при росте истории и частоте тиков. | Измерить 6/20/50 инструментов и длинную историю; вынести query-level агрегацию realized/commission, загрузку только открытых лотов либо поддерживаемый ledger aggregate лишь при доказанном выиграше. Передавать в одном transaction/batch необходимые данные; не использовать устаревший hydrated snapshot вместо authoritative ledger. | SQL count/decision, p95 persist и RSS сравниваются на одинаковом dataset. LIFO, частичные комиссии, delayed fills и гонка hydration/execution остаются корректны. |
| P1 | Audit A5: неполный replay, future/stale стакан; raw возраст теряет знак. | Сохранять source timestamps/snapshot ID и входы решения; определить clock/freshness contract, связать задержки quote→decision→publish с reason_code. Сначала диагностировать часы/NTP и источник, не «омолаживать» старый стакан. | Offline replay даёт то же решение; проверены future/stale границы; наблюдаемость не раскрывает broker credentials. |
| P2 | `storage/repository.py` Worker совмещает intent lifecycle, LIFO, bootstrap, outbox, кеш команд и decision persistence. | После стабилизации выделять только подтверждённые связные ответственности: intent transitions, ledger calculations и outbox query/publication. Сохранить единую транзакцию через существующий Unit of Work/Session; бизнес-инварианты не должны зависеть от HTTP. | Старые интеграционные тесты без переписывания expected behavior; атомарность fill+lot+fact и graceful recovery не меняется. SOLID не повод вводить неиспользуемые интерфейсы. |
| P2 | `src/moex_sentinel/storage/database.py`: PostgreSQL pool 5 + overflow 10 на engine; несколько процессов/engines умножают бюджет соединений. Реальные IOPS/CPU сервера неизвестны. | После выбора сервера измерить активные соединения, DB wait, fsync и pool wait; определить общий connection budget. Менять pool/ресурсы только по bottleneck, не увеличивать workers blindly. | Нет pool timeouts и превышения connection budget; p95 ingress улучшается при одинаковой нагрузке. Worker SQLite остаётся единственным владельцем. |
| P2 | Audit A6: интервал с названием 24h начинается существенно раньше 24 часов; в рамках recovery исправляется выбор baseline и полнота периода. | После интеграции recovery закрепить contract `from/to/complete` регрессиями и мониторингом реального интервала, отдельно от производительности. | Ряд без пропусков и с длительным разрывом возвращает честное покрытие периода. |

## Последовательность измерения

1. Использовать существующий `develop/benchmarks/README.md` и runner `develop/scripts/profile_postgresql.sh`: он создаёт отдельную PostgreSQL и синтетический рынок/брокера. Рабочую БД не подключать.
2. Снять baseline до оптимизации на 6, 20 и 50 инструментах: decision tick, SQL count, core ingress, commit, HTTP, outbox drain, p50/p95/p99 и RSS. Не складывать вложенные длительности или percentile разных распределений.
3. Добавить отдельные сценарии backlog, длинная LIFO история, delayed fill, восстановление после потери HTTP-ответа. Текущий benchmark — последовательный producer/drain и PostgreSQL tmpfs: он не измеряет fsync постоянного production-диска и не доказывает SLA.
4. На отдельном тестовом окружении целевого сервера повторить с постоянным SSD и тем же dataset, сохранив тип CPU, архитектуру, версии образов, объём данных и sample count. Установить целевые пороги с владельцем сервера после baseline.
5. Оптимизировать один bottleneck за раз, повторять только соответствующий эксперимент и регрессионные проверки. Перенос SQLite на PostgreSQL, Redis, Kubernetes или горизонтальное масштабирование пока не обоснованы измерениями.

## Ограничения текущей подготовки

Не выполнены тесты на целевом сервере, network latency к брокеру, rehearsal restore, disaster recovery и нагрузка на постоянном диске. Не настроены внешний domain/auth, off-host backup scheduler и alert destination — эти параметры зависят от выбранного хоста и требований владельца. Deployment overlay уменьшает риски запуска, но не заменяет эти проверки.
