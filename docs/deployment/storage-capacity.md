# Оценка ёмкости хранения на 30 и 180 дней

> **Первый измеренный baseline, без суточного прогноза.** 02.10.2026 доступ
> к VPS восстановлен и read-only замеры выполнены. Ниже отдельно приведены
> фактический снимок, короткая экстраполяция и прежние условные сценарии.
> Наблюдение 24 часа/7 суток ещё не завершено; retention и хранилища не менялись.

## Цель и границы

Оценивать PostgreSQL Core, SQLite Worker, Docker logs, WAL, резервные копии,
образы и временные файлы как разные потребители диска. Для каждой категории
сравнивать отдельно:

1. **Валовую генерацию** — сколько байт записывается за период до ACK, удаления,
   checkpoint, ротации или сжатия.
2. **Удерживаемый объём** — сколько данных остаётся с действующей политикой
   очистки и ретеншна.
3. **Пиковый лимит/запас** — максимальный конфигурационный предел и свободное
   место на файловой системе с учётом временных копий и восстановления.

Единицы в расчётах ниже — KiB/MiB/GiB (степени 1024). Они не заменяют фактическую
занятость в байтах. Цель метода — сначала получить повторяемую базовую линию,
затем выбрать безопасные политики хранения.

## Фактический baseline — 02.10.2026

Сбор выполнен только на чтение: основные метаданные 01.10 UTC
23:17:14–23:17:16 (02.10 МСК), journald 23:18:10, дерево релиза 23:22:52.
Root независимо повторил host metadata в 23:17:30 и volumes/Backend logs
в 23:23:24. Значения меняются во время работы; снимки не атомарны между
сервисами. Evidence с правами 700/600:
`develop/reports/vps-audit-20261002/storage-baseline.json`,
`host-recheck.json`, `storage-root-recheck.json` в том же каталоге.

| Категория | Измерено | Что входит |
| --- | ---: | --- |
| Диск VPS | 55,99 GiB всего; 47,45 GiB доступно; 5,68 GiB занято | `statvfs`; available исключает зарезервированные блоки |
| Оперативная память | 1,92 GiB всего; 0,79 GiB available | Моментный снимок; swap занят примерно на 0,13 GiB |
| PostgreSQL volume | 238 874 624 B / 227,81 MiB allocated | Весь volume, включая cluster/WAL; не складывать с размером БД |
| Worker volume | 60 481 536 B / 57,68 MiB allocated | SQLite и sidecar-файлы |
| Docker logs, 6 контейнеров | 54 378 496 B / 51,86 MiB allocated | Текущие и ротированные файлы; Backend 34,75 MiB, Worker 14,64 MiB |
| Journald, весь VPS | ≈604 504 064 B / 576,50 MiB | Агрегат системного журнала, не только приложение |
| Nginx logs | 81 920 B / 0,08 MiB allocated | Отдельно от Docker log cap |
| Единственный pre-import Core dump | 77 533 B файла; каталог 86 016 B allocated | Не текущая совместная копия Core/Worker; restore не проверен |
| Текущее дерево релиза r2 | 5 951 488 B / 5,68 MiB allocated | Разыменованная цель `current`; symlink сам занимает только 54 B apparent |

Docker metadata: все шесть контейнеров running, restart count 0; четыре имеют
healthy healthcheck, у двух Worker healthcheck отсутствует. Ко всем шести
**применён** `json-file max-size=10m, max-file=5`; срок удержания в днях ещё
не измерен. Docker CLI отдельно сообщает images 1,206 GB и build cache
910,9 MB (округлённые decimal значения, общие слои могут пересекаться).
Образы предыдущего релиза/volumes не удалялись.

### Короткий рост Core: предварительный сценарий

`core-broker.json` и `core-delivery-after.json`: `pg_database_size` вырос с
161 094 679 до 164 224 023 B между 23:11:38,939 и 23:16:46,686 UTC.
Дельта **3 129 344 B за 307,747 сек**, или **0,582 MiB/мин**.

Если этот темп сохранится круглосуточно без изменения политики:

| Период | Дополнительный объём только БД Core |
| --- | ---: |
| 30 дней | ≈24,55 GiB |
| 180 дней | ≈147,28 GiB |

Это экстраполяция одного пятиминутного отрезка, **не измеренный месячный расход
и не прогноз всего VPS**. Worker, WAL, логи, backups, изменение нагрузки,
checkpoint/reuse и запас восстановления учитываются отдельно. Из неё нельзя
выводить точную дату заполнения диска.

Главные Core relations в первом снимке: `automation_events` 99 573 760 B
и `trade_audit_events` 45 645 824 B, включая индексы/TOAST. Решений 2610,
последнее в 20:50 UTC; при этом стадии `POSITION_RECONCILIATION_STARTED` и
`POSITION_RECONCILED` продолжают добавляться при `STRATEGY_ENABLED=false`.
Следующее исследование S1 должно разделить повторяющийся аудит сверки,
обязательные Core facts и диагностические логи. До проверки replay-контракта
не сокращать частоту фактов и не удалять историю по возрасту.

## Исходное покрытие

| Область | Класс | Что известно сейчас | Что измерить |
| --- | --- | --- | --- |
| Стратегия и позиции | Подтверждён runtime | Running `StrategySettings.enabled=false`; 6 holdings сверены broker/Core/Worker. Интервал Worker по умолчанию 1 сек. | Частота решений/сверки за суточное окно, а не только период итерации. |
| Частота решений | Выведено из кода/допущение | Dedup одинаковых кадров означает, что `6 holdings × 1 iteration/sec` не доказывает 6 решений или 6 записей в секунду. В таблице сценариев ниже частоты заданы отдельно как условия. | Число уникальных decisions и WAIT по каждой автоматизации за период. |
| Worker business audit | Подтверждено по коду | Один `record_decision_process` сохраняет 6 стадий: 5 общих и одну из `BROKER_INTENT_CREATED` / `TRADING_STEP_COMPLETED`. См. `src/trading_automaton/services/business_audit.py:86-169`. | Реальное число вызовов, байты строк и физический рост SQLite. |
| Core facts/outbox | Подтверждено по коду | Decision business audit и Core fact envelopes/outbox — отдельные записи и отдельные хранилища; это write amplification сверх шести Worker audit rows. Доставленный ACK-prefix удаляется из Worker outbox; долговременная политика очистки pending/failed по возрасту не определена. См. `src/trading_automaton/storage/models.py:78-97`, `storage/repository.py:1408-1445`. | Количество Core envelopes по типам, ACK/FAILED/PENDING, размер таблиц и индексов. |
| Portfolio snapshots | Подтверждённое расписание, условное число | Интервал 60 сек даёт **43 200** snapshot rows за 30 дней и **259 200** за 180 дней на один непрерывно успешный account-currency stream. `portfolio_snapshot_runs` считаются отдельно. Ошибки, число счетов и валют меняют итог; см. `src/moex_sentinel/config.py:24-27`, `storage/models/trading_analytics.py:52-80`. | Фактические успешные строки, runs, error rows, счёта/валюты и средний физический размер. |
| Docker logs | Применённый cap и baseline измерены | Все 6 containers используют `json-file`, `max-size=10m`, `max-file=5`: номинально около 300 MB по конфигурации суммарно; allocated baseline 51,86 MiB. См. `deploy/remote/compose.remote.yml:2-8,10-44`. | Суточная генерация и фактическое время удержания; только metadata, без file contents. |
| PostgreSQL / SQLite / WAL | Первые снимки измерены | Известны database/relation sizes, volumes, DB/WAL/SHM и page/freelist baseline. Отдельный устойчивый WAL peak ещё не измерен. | Суточный рост и checkpoint/reuse; без payload в отчёт. |
| OS/systemd, Nginx logs | Агрегаты измерены | Вне Compose cap; см. baseline выше. | Независимые настройки ротации и суточный темп. |
| Backups, images, filesystem headroom | Первый baseline измерен | Известны текущие размеры и available bytes; расписание backups и restore headroom ещё не приняты. | Объём каждого поколения backup/image и пик создания/восстановления. |

Снимок от 02.10 подтверждает состояние только на указанное окно. Он не заменяет
новую runtime проверку в следующей сессии и суточный/недельный ряд S1.

## Сценарии частоты решений

Это верхнеуровневые **условные** сценарии для шести позиций. Они предполагают,
что частота решений удерживается все календарные сутки; фактическая нагрузка
уменьшается пропорционально активному времени торгов и паузам. Интервал Worker
1 сек — только период итерации, не источник частоты решений.

| Условие | Логических решений / 24 ч | / 30 дней | / 180 дней | Worker audit rows / 24 ч (6 на решение) | / 30 дней | / 180 дней |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 6 позиций × 1 решение в минуту на позицию | 8 640 | 259 200 | 1 555 200 | 51 840 | 1 555 200 | 9 331 200 |
| 6 позиций × 1 решение в секунду на позицию | 518 400 | 15 552 000 | 93 312 000 | 3 110 400 | 93 312 000 | 559 872 000 |

Следующая таблица — **только иллюстрация валовой сериализованной генерации
шести Worker audit records на решение**, при условном размере каждого record
1 KiB или 5 KiB. Она не включает Core envelopes/outbox, индексы, TOAST, WAL,
внутреннюю фрагментацию, retry-записи, backups и другие таблицы. Это не прогноз
физического объёма БД и не фактический расход VPS.

| Частота решений | Период | Audit rows | Валовая генерация при 1 KiB/row | При 5 KiB/row |
| --- | --- | ---: | ---: | ---: |
| 1/мин на каждую из 6 позиций | 24 ч | 51 840 | ≈0,05 GiB | ≈0,25 GiB |
| 1/мин на каждую из 6 позиций | 30 дней | 1 555 200 | ≈1,48 GiB | ≈7,42 GiB |
| 1/мин на каждую из 6 позиций | 180 дней | 9 331 200 | ≈8,90 GiB | ≈44,5 GiB |
| 1/сек на каждую из 6 позиций | 24 ч | 3 110 400 | ≈2,97 GiB | ≈14,8 GiB |
| 1/сек на каждую из 6 позиций | 30 дней | 93 312 000 | ≈89,0 GiB | ≈445 GiB |
| 1/сек на каждую из 6 позиций | 180 дней | 559 872 000 | ≈534 GiB | ≈2 670 GiB |

Реальное число записей на решение может отличаться от 6, если изменится
контракт `BusinessAuditService`, добавятся order/reconciliation stages или
возникнут повторы. На размер решения влияют и отдельные Core envelope/outbox
записи. Их считать независимо по фактическим строкам и тем же временным окнам.

### Снимки портфеля

При 60-секундном интервале:

| Период | Успешные snapshot rows на один account-currency stream | Runs |
| --- | ---: | --- |
| 24 ч | 1 440 | Отдельный ряд на запуск коллектора; число зависит от расписания и retry/error. |
| 30 дней | 43 200 | Считать по `portfolio_snapshot_runs`, успехи и ошибки отдельно. |
| 180 дней | 259 200 | Считать по `portfolio_snapshot_runs`, успехи и ошибки отдельно. |

Умножать rows на число account-currency streams можно только после измерения
того, сколько счетов и валют реально возвращает брокер в одной успешной
коллекции. Один run может завершиться с ошибками или неполным набором snapshots.

## Модель полного расхода

Для каждой категории `i`:

```text
logical_generation_i(T) = generated_rows_i(T) × mean_logical_bytes_per_row_i
logical_retained_i(T) = logical_baseline_i
                        + newly_persisted_rows_i(T) × mean_logical_bytes_per_row_i
                        - logically_pruned_rows_i(T) × mean_logical_bytes_per_row_i
physical_baseline_i = измеренный allocated/physical размер на начале окна
physical_growth_rate_i = измеренная дельта allocated/physical bytes / elapsed_time
physical_forecast_i(T) = physical_baseline_i + physical_growth_rate_i × T
disk_peak_i(T) = physical_forecast_i(T)
                 + отдельно измеренные WAL/SHM/checkpoint/rewrite transients
                 + backup staging и restore workspace
```

`logical_retained` считается по строкам и их логическому размеру, не вычитается
из физического размера. ACK outbox удаляет доставленные строки логически, но сам
по себе не уменьшает выделенный размер файла/volume и не возвращает место ОС.
SQLite может оставить освобождённые страницы во freelist; PostgreSQL `VACUUM`
может освободить dead tuples для повторного использования без немедленного
уменьшения файлов на диске. Поэтому физический forecast опирается на измеренный
рост allocated bytes за окно, где уже проявились write amplification, reuse,
vacuum/checkpoint и штатная ротация. Не считать logical deletion вычитанием из
physical forecast. Пиковые временные файлы учитывать отдельно, чтобы не смешать
рост retained data и кратковременный restore/backup peak.
Отрицательная дельта после ротации/сжатия не означает бесконечного уменьшения:
её не экстраполировать линейно на180дней. Для bounded категорий использовать
наблюдавшийся диапазон/подтверждённый cap; для растущих — диапазон устойчивого
неотрицательного прироста. Прогноз не может уйти ниже необходимого сохранённого
состояния и должен показывать пиковые расходы отдельно.

Суммарная потребность:

```text
disk_peak = PostgreSQL(heap + indexes + TOAST + WAL)
          + Worker(SQLite DB + WAL + SHM)
          + Docker logs
          + OS/systemd/journald logs + Nginx logs
          + backup sets + temporary backup/restore space
          + images/layers + system/application files (excluding separately counted logs)
          + headroom
```

Уровни расчёта не смешивать: logical rows, физический размер таблиц, размер
volume, размер файловой системы и объём off-host backups — разные значения.
Планировать свободный запас относительно измеренного пика; конкретный процент
запаса устанавливать после получения размера VPS-диска и согласованного
восстановительного сценария. Сжатие Docker logs, PostgreSQL backup или архивов
заранее не считать.

## Метод измерения

Измерения разделить на два контура:

1. **Операционный, только чтение:** короткие снимки метаданных/счётчиков и
   физического размера текущих файлов. Не менять настройки, не выполнять
   `VACUUM`, `CHECKPOINT`, `wal_checkpoint`, `ANALYZE`, cleanup, restart,
   rotation, deploy или записи в БД. Не выбирать и не выводить payload, не
   сохранять/печатать logs, не выводить токены или финансовые значения в capacity
   report. Финансовая сверка проводится отдельно по правилам торгового аудита.
2. **Изолированные тесты:** отдельные тестовые PostgreSQL и Worker SQLite без
   рабочих volumes, сервисов, брокерских credentials и внешних ордеров. На
   синтетических данных проверять рост, ретеншн, backup/restore и возможную
   агрегацию. Сохранять старые и новые итоговые ряды для сравнения.

### Операционное окно: первые 24 часа

- До начала и по завершении окна записать timestamp UTC, идентификатор релиза,
  сервис, размер volume/filesystem и свободные байты. Метрики таблиц и
  счётчики сохранять как числа без содержимого строк.
- PostgreSQL: снять `pg_database_size(current_database())`,
  `pg_total_relation_size()` для таблиц с indexes/TOAST, и размеры WAL отдельно.
  Для сравнений фиксировать `pg_stat_user_tables.n_tup_ins`, `n_tup_upd`,
  `n_tup_del`, `n_live_tup`, `n_dead_tup` и `pg_stat_database.stats_reset`.
  Считать дельты между снимками; не сбрасывать статистику. В выборке для оценки
  row payload разрешать только ограниченные по timestamp выборки
  `pg_column_size(row)`; не выбирать/печатать сами payload или account data.
- SQLite Worker: читать только через короткое read-only подключение; записать
  размеры файлов `*.db`, `-wal`, `-shm`, затем `PRAGMA page_count`,
  `PRAGMA page_size`, `PRAGMA freelist_count`. Не выполнять checkpoint/backup в
  этом замере и не удалять sidecar-файлы. Размер WAL может зависеть от активных
  соединений.
- Docker logs: через Docker Engine API инспектировать только контейнеры разрешённого
  compose project: `LogConfig`, `LogPath`, безопасные project/service labels.
  Для текущего и rotated `json-file` ограничиться bounded filesystem `stat`:
  logical bytes и allocated blocks, file count и возраст/rotation metadata.
  Contents не читать, файлы не редактировать, не ротировать и не удалять. Не
  следовать путям для других containers. Engine logs stream годится отдельно как
  оценка logical generation/throughput, но не как физический размер JSON logs:
  он не отражает metadata framing, rotated files и allocated disk blocks.
- OS/systemd/journald и Nginx logs: измерить размеры и allocation metadata
  относящихся к этим службам файлов/журналов и проверить их независимые настройки
  ротации. Они не ограничены Compose `json-file` cap; без просмотра содержимого.
- Снять байты всех backups, образов и filesystem headroom отдельно. Рост за 24 ч
  — одна точка наблюдения, а не устойчивый темп.

Для каждого PostgreSQL снимка полезны функции `pg_database_size` и
`pg_total_relation_size` из [PostgreSQL 16 System Administration Functions](https://www.postgresql.org/docs/16/functions-admin.html)
и статистика таблиц из [PostgreSQL 16 Cumulative Statistics System](https://www.postgresql.org/docs/16/monitoring-stats.html).
Размер строки не отражает полного расхода: индексы и TOAST считать отдельно;
счётчики статистики собирать как интервальные дельты без `pg_stat_reset`.

### Подтверждение: семь суток

Повторить те же read-only измерения не реже одного раза в сутки семь суток,
захватывая рабочие и нерабочие торговые дни, snapshots, ротацию логов, restart
события и обычные retry. Перед сравнением проверить `stats_reset` и перезапуски,
чтобы не интерпретировать сброшенные счётчики как отрицательный рост.

Для каждого ряда показать начальный/конечный объём, p50/p95 суточного прироста,
наибольший суточный прирост и причины скачков (например, bootstrap, миграция,
backup или WAL/checkpoint). Экстраполировать на 30/180 дней только диапазоном
на основе наблюдавшихся диапазонов нагрузки; указать сезонность, торговые часы,
ошибки, число account-currency streams и долю повторов. Сопоставить модель с
измеренным ростом relations/files; не объявлять прогноз точным при расхождении.

### Изолированные тестовые измерения

Поднять отдельную disposable PostgreSQL 16 и временный каталог SQLite; не
подключать рабочие DB/volumes и credentials. На фиксированных synthetic seeds
воспроизвести оба decision-rate сценария, snapshots на 1 и нескольких
account-currency streams, ACK, pending failure/retry, WAL и backup staging.
Снимать те же физические показатели. Явно подписать, что это тестовая модель,
а не оценка текущего VPS. Удалять только тестовые контейнеры/данные после
проверки, что их mounts изолированы.

SQLite WAL ведёт себя отдельно от главного DB-файла и checkpoint может
переносить страницы из WAL в DB; в read-only окне поэтому измерять `.db`, WAL и
SHM совместно и не запускать checkpoint. См. [SQLite Write-Ahead Logging](https://www.sqlite.org/wal.html)
и [SQLite PRAGMA](https://www.sqlite.org/pragma.html) для `page_count`,
`page_size` и `freelist_count`.

## Вехи и критерии готовности

| Веха | Результат и критерий завершения | Статус |
| --- | --- | --- |
| S1 — измерение | Отчёт содержит actual physical bytes/allocated blocks и logical row counts/rates для PostgreSQL, Worker SQLite, Docker и host logs, snapshots, backups/images; раздельно показывает baseline, 24 ч и 7 суток, диапазоны 30/180 дней, удержание и peak/headroom. Описаны scope, permissions, timestamp, ограничения и расхождения между рядами и ростом файлов. | В работе: baseline 02.10 и короткий темп Core измерены и перепроверены. Суточного/недельного ряда ещё нет; сбор по расписанию не установлен. |
| S2 — оптимизация | Подготовить предложения на основе S1. Каждое предложение сначала проходит isolated equivalence/restore tests, включая сохранение summary, ACK/replay, audit и восстановления; только после отчёта и отдельной приёмки можно обсуждать применение. | Не начата. Изменений retention, схемы или runtime нет. |

Эти вехи пока не закрыты. Baseline и метод не заменяют суточный/недельный S1
report и не являются доказательством готовой capacity plan.

## Варианты retention для обсуждения

Это предложения для последующего решения, а не текущие политики.

| Данные | Предложение | Условия |
| --- | --- | --- |
| Контейнерные логи | Целевое локальное удержание — 7–14 дней, оперативные error/warn логи при необходимости централизованно удерживать 30 дней. | Сначала измерить темп и проверить, достаточно ли текущего `10m × 5`; cap может соответствовать гораздо меньшему сроку. Это approximate config cap (~50 MB/service), не physical measurement. Remote storage, encryption, доступы и фактическое время удержания выбрать отдельно. |
| Host logs | Для systemd/journald и Nginx определить отдельное retention; operational error/warn history можно централизованно удерживать 30 дней. | Эти логи не входят в Compose `json-file` cap. До оценки не менять rotation или vacuum policy journal. |
| Portfolio snapshots | Сохранять полную детализацию минимум за 180 дней фактической истории; downsample обсуждать только для истории старше 180 дней. При любой агрегации сохранять контрольные точки и latest point ≤ требуемой границы каждого P&L окна. | `TradingSummaryService` запрашивает окна 1/7/30 дней по currency (`src/moex_sentinel/services/trading_summary.py:35-91`); `common_baselines` ищет один общий run со всеми account-currency snapshots текущего latest set, сначала последний `captured_at ≤ boundary`, если такого нет — самый ранний общий run (`src/moex_sentinel/storage/repositories/portfolio_snapshots.py:178-225`). Если common run не найден, P&L value отсутствует и `complete=false`; если выбран baseline вне окна точности, delta может вернуться с `complete=false`; earliest fallback после boundary тоже неполон (`src/moex_sentinel/domain/trading_summary.py:156-175`). Сохранять требуемые account-currency points, run rows и FK; downstream summaries должны совпадать на изолированном тесте. |
| `portfolio_snapshot_runs` | Retention отдельно от snapshot rows; сохранять агрегаты ошибок и длительности после согласования аудиторской потребности. | Runs отражают пропуски и неуспешные сборы; удалять их синхронно с FK и проверенной диагностикой. |
| Worker audit и Core facts | Не удалять по возрасту до доказанного replay-контракта. | `BusinessAuditService`/Core envelopes/outbox имеют разные владельцы и назначение; обеспечить чтение и сопоставление по process/event IDs. |
| Decisions, automation events, dedup IDs, repair journal, intents/orders/executions | Не применять age-based prune. Архивирование обсуждать только после FK/replay проверки и успешного restore в изолированной среде. | Эти записи являются основанием подтверждённого торгового состояния и восстановления, не просто диагностическими логами. |
| Worker outbox | ACK rows удаляются штатно; pending/failed сохранять до явного разрешения причин, не добавлять retention по возрасту. | Наблюдать количество, возраст старейшей строки, retry count, delivery state и disk growth. |
| Backups | Определить ограниченное число поколений по RPO/RTO; хранить копию вне VPS, приватно и зашифрованно. | Core PostgreSQL и Worker SQLite должны образовывать совместимую точку восстановления. До ротации старых поколений сделать restore-test в отдельных volumes без брокерских credentials и ордеров; проверить целостность и сопоставление Core/Worker. |

PostgreSQL `DELETE` не гарантирует немедленного возврата места ОС: обычный
`VACUUM` освобождает dead tuples для повторного использования, а `VACUUM FULL`
переписывает relation и требует дополнительного места/блокировки. Не использовать
`VACUUM FULL` как автоматический ответ на рост; сначала измерить dead tuples,
indexes, WAL и влияние на сервис. См. [PostgreSQL 16 VACUUM](https://www.postgresql.org/docs/16/sql-vacuum.html).

## Порядок оптимизации

1. Снять 24-часовую read-only baseline и подтвердить seven-day trend.
2. Проверить вклад repeat frames, неизменившихся WAIT решений, audit stages,
   envelopes/outbox, snapshots, indexes/TOAST и WAL по отдельности.
3. Рассматривать агрегацию неизменных WAIT/snapshot данных только в тестовом
   контуре. Она должна сохранять последнюю актуальную оценку/snapshot, baseline
   `TradingSummaryService`, `sequence_number`/ACK/replay/idempotency и требуемую
   audit-семантику. Если для доказательства нужна потеря исходного события,
   агрегация не подходит.
4. Затем оценить сжатие/вынесение архивных логов и согласованный snapshot
   downsample. Не считать ожидаемую экономию до повторного измерения.
5. Partitioning, изменение схемы или новых metrics/storage компонентов касаться
   только если измерение показывает конкретный bottleneck; отдельно проверить
   транзакционные границы Core и Worker.
6. Принять лимиты/alarms по измеренному peak плюс restore headroom, а не по
   номинальным log caps или условным KiB на запись.

## Что остаётся неизвестным

- Суточный/недельный тренд VPS filesystem/volumes и available bytes.
- Фактическое время удержания локальных логов; central logging не установлен.
- Суточная частота уникальных решений и audit rows при `STRATEGY_ENABLED=false`.
- Размер Worker audit rows, Core fact payloads, table/index/TOAST growth и WAL.
- Снимки/ошибки по фактическим account-currency streams, число runs и retention.
- Размер/частота backups, image layers, restore staging и допустимые RPO/RTO.
- Приемлемые пороги алертов и требуемая история финансовых/аудиторских данных.

## Официальные источники

- [Docker Engine: JSON File logging driver](https://docs.docker.com/engine/logging/drivers/json-file/) — `max-size`, `max-file` и ротация.
- [PostgreSQL 16: System Administration Functions](https://www.postgresql.org/docs/16/functions-admin.html) — `pg_database_size`, `pg_total_relation_size`.
- [PostgreSQL 16: Cumulative Statistics System](https://www.postgresql.org/docs/16/monitoring-stats.html) — статистика по таблицам и счётчики.
- [PostgreSQL 16: VACUUM](https://www.postgresql.org/docs/16/sql-vacuum.html) — повторное использование пространства и ограничения `VACUUM FULL`.
- [SQLite: Write-Ahead Logging](https://www.sqlite.org/wal.html) — WAL/checkpoint behavior.
- [SQLite: PRAGMA](https://www.sqlite.org/pragma.html) — read-only page/freelist indicators.

## Проверка документа

Автор `/root/sandbox_probe` (gpt-6-luna); root проверил исходники/арифметику,
`/root/sandbox_final_review` независимо проверил метод измерения, ownership,
retention/replay и восстановление: PASS после исправления смешения logical
и physical bytes, Docker stream/дискового объёма и account-currency baseline.
Baseline 02.10 собрал `/root/sandbox_probe`, root перепроверил SSH/sudo,
host/volumes/log metadata и арифметику короткого роста Core. Независимое ревью
дополнения и отчёта `/root/sandbox_final_review` — PASS, существенных замечаний
нет. Ограничение: 24ч/7суток S1 не завершены;
политики S2 не применены.
