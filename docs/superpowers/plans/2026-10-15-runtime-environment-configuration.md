# ENV-конфигурация сервисов и автоподготовка базы

**Статус 15.10.2026: LOCAL DONE.** ENV1–ENV4 реализованы, проверены и приняты независимым кросс-ревью. ENV5: deployment и активация daily выполнены 15.10 в 19:07 МСК; торговая приёмка остаётся открытой.

**Цель:** эксплуатационные настройки читаются из ENV, валидируются при старте и доходят до потребителей. Каждый параметр описан в [каталоге ENV](../../configuration/environment.md). PROD-шаблон явно включает подготовку базы дня; фактическая активация на VPS выполняется отдельно.

**Дизайн и контракт параметров:** [спецификация](../specs/2026-10-15-runtime-environment-configuration-design.md). Сохраняется существующая цепочка Settings → composition → параметры сервисов. Для Analytics добавлен небольшой `AnalyticsSettings`. Новых зависимостей и миграций нет.

## Сверка актуальности и границы

Перед декомпозицией root сверил код HEAD `2f25bfd`, очередь, планы запуска PROD, Settings, Compose и шаблоны. Предыдущую реализацию пользователь уже закоммитил; четыре исходных изменения планирования/очереди сохраняются. Найдены неполная передача существующих настроек в Compose, эксплуатационные тайминги в конструкторах, отсутствие общего каталога и игнорирование настроек пула БД в HTTP lifecycle Core.

Пользователь подтвердил scope: **ENV, документация и автоподготовка базы**. Поддержка нескольких UID и автоматический ARM отложены. Планировщик подготовки дня уже существовал; он расширен настройками и профилем применения. Последняя read-only проверка PROD 15.10 в 09:39 МСК показала Core healthy и фактический флаг daily `false`. Исторические отчёты не подтверждают более позднее состояние runtime.

Приложение и Compose сохраняют default daily `false`. PROD-пример задаёт daily `true` и пустой canary UID; `READ_ONLY` и выключенная стратегия остаются безопасными значениями примера. Подготовка базы не выдаёт ARM, не снимает HOLD/STOP и не отменяет recovery. Доменные данные и протокольные ограничения не становятся произвольными ENV-настройками.

В рамках задачи запрещены новые ветки/worktree, stage/commit/push, SSH/deploy, изменение приватного ENV, ARM/Resume и реальные торговые операции/уведомления. Тесты БД выполняются только на отдельном локальном экземпляре.

## ENV1 — Core

**Автор:** env_core_author. **Независимый рецензент:** env_core_reviewer.

Область: Core Settings, composition и HTTP lifecycle; daily preparation, readiness alerts, market recovery, Telegram и Analytics version adapter; связанные config/service/adapter/API tests.

- [x] RED/GREEN: нестандартные ENV-значения доходят до потребителей; неверные тайминги, NaN/Inf и пустые retry schedules отклоняются.
- [x] Переданы настройки пула БД в HTTP lifecycle; сохранены прежние значения по умолчанию и контракты сервисов.
- [x] Целевые проверки и независимое ревью пройдены. Авторский пакет: 177 PASS, замечаний C/I нет.

## ENV2 — Worker и Analytics

**Автор:** env_worker_analytics_author; ограниченный follow-up URL — env_core_author. **Рецензенты:** env_configuration_architect, затем env_core_reviewer.

Область: Worker Settings/composition/Analytics broker, новый Analytics Settings и создание HTTP-клиента; связанные тесты конфигурации и runtime.

- [x] HTTP timeouts, broker recovery interval и TTL кэша счёта читаются из ENV; Analytics URL/timeout валидируются и передаются клиенту.
- [x] Значения по умолчанию, broker identity, READ_ONLY и границы сервисов сохранены.
- [x] Исправлено I1: Worker отклоняет пустые/не-HTTP URL, userinfo и некорректный host/port при старте. RED/GREEN: 16 FAIL → 95 PASS; независимое повторное ревью закрыло I1.

## ENV3 — Compose, шаблоны, документация

**Автор:** env_deployment_docs_author. **Независимый рецензент:** env_core_reviewer.

Область: три Compose-файла, три ENV-шаблона, серверная настройка Vite dev proxy, каталог и runbooks, проверки покрытия и рендеринга.

- [x] Settings передаются через Compose. Core `CORE_ANALYTICS_URL` отделён от Worker `ANALYTICS_URL`; секреты и контуры не смешиваются.
- [x] Сохранены topology, volumes и обязательный сбор обоих контуров общим remote Collector.
- [x] Документированы назначение, тип/единица, default/проверка, сервис, секретность, перезапуск и пример каждого параметра; отдельно описаны фиксированные контракты.
- [x] PROD-пример явно включает только daily baseline; описаны изменение приватного ENV, контролируемый перезапуск Core и проверка текущего дня без ARM/Resume.
- [x] Compose/coverage: 16 PASS; Vite config: 9 PASS. Замечание M1 о native defaults/Compose requirements/значениях шаблонов исправлено и независимо закрыто.

## ENV4 — итоговая интеграция

**Ответственный:** root. **Независимое итоговое ревью:** env_core_reviewer, автором интеграции не является.

- [x] Исходные пользовательские изменения и посторонний PNG сохранены; Git mutations и операции на VPS не выполнялись.
- [x] Frontend: 275 PASS, typecheck и production build PASS. Вспомогательные тесты: 141 PASS на отдельной локальной PostgreSQL.
- [x] Начальный полный backend выявил старую классификацию `market_analytics.config` как pure. Исправлен только архитектурный guard: config может использовать Settings, pure-слой не может импортировать config/Settings. Добавлены положительные и отрицательные fixtures; 30 PASS, независимое ревью C0/I0/M0. Guard проверяет импорты, а не произвольное чтение ENV через stdlib.
- [x] Ruff/Black: 28 изменённых Python-файлов, exit 0.
- [x] Повторный полный backend: **2617 PASS**, 376.56 s, exit 0, отдельная локальная PostgreSQL. Все 506 src/tests Python-файлов неизменны во время прогона; ещё 27 alembic/develop test inputs неизменны относительно предыдущей фиксации.
- [x] Независимое итоговое ревью ENV1–ENV3 и архитектурного follow-up: C0/I0/M0; существенных открытых замечаний нет.
- [x] Итоговые доказательства, отчёт, очередь и локальная точка продолжения обновлены. Локальный пакет: `develop/reports/runtime-environment-configuration-20261015/`, итоговый `report.md`, `verification/results.json`. Существенных открытых замечаний нет.

## ENV5 — deployment и проверка торговли (15.10)

По новому явному поручению пользователя проверенный checkout выпущен до merge:
`vps-20261015-env-4a71589e8766`, Core/Worker/Analytics. Frontend, shared PostgreSQL,
Collector и остановленный TEST сохранены. Новых миграций нет. Приватный ENV
изменён только daily=false→true; credentials/TRADE/strategy/пустой canary сохранены.

- [x] Fresh runtime preflight и package/source manifest, exact base image IDs.
- [x] Независимое pre-execution review C0/I0/M0; 5 mocked safety tests и 22 observation tests PASS.
- [x] Offline сборка/импорты и strict Compose diff; controlled Worker stop exit0 → штатный owner-stopped → новый owner generation5/revision8 в recovery-only.
- [x] Actual image/source hashes, health/ping/UI, Worker progress, unchanged volumes/TEST и read-only admission/operations проверены. Deployment acceptance PASS.
- [x] Daily создала базу за 15 октября в 19:06:54 МСК; BASELINE_REQUIRED устранён.
- [x] Итоговое независимое env_core_reviewer review: C0/I0/M0. Proof: `develop/reports/runtime-env-deploy-20261015/{report.md,review-final.md,verification.json}`.

Торговля не началась: на 19:08:49 МСК счёт STOP_REQUIRED/recovery-only,
DISARMED и RECONCILIATION_REQUIRED; ARM/limits/reconciliation отсутствуют.
Обе позиции IN_WORK, повторный Resume не требуется. В полученной bounded
broker-operations выборке новых операций после выпуска нет; persisted order
facts напрямую не читались. Diagnostics aggregate UNKNOWN не объявляется PASS;
Core/Analytics container health и Worker progress проверены отдельно.

Merge выполняет пользователь. Далее PT7: явная recovery/сверка выбранного
инструмента и счёта, утверждённые UID/лимиты и отдельный ARM по действующему
регламенту. Deployment и база дня не заменяют торговую приёмку. Multi-UID и
auto ARM отложены.

## ENV6 / PT7 — снять recovery-only (15.10, DONE)

Пользователь отдельно поручил выполнить сверку. Один штатный CANARY_UID reconcile
GOLD в 19:20:25 МСК получил CANARY_READY/can_arm=true и нулевые active orders,
stop orders, unresolved reservations. API instrument details подтвердил внешний
UID; ошибка смешения внутреннего catalog ID и внешнего UID в ops helper исправлена
до исполнения (RED2FAIL/1PASS → GREEN3PASS, независимое повторное ревью).

Свежий GET подтвердил account DISARMED/recovery_only=false/revision9; owner/gen5,
позиции и настройки сохранены. Повторный GET в 19:21:50 МСК после TTL подтвердил,
что recovery не вернулся. Worker OK, outbox CLEAR. ARM/limits отсутствуют,
торговля не активирована; expiry сверки добавляет RECONCILIATION_REQUIRED,
не возвращая recovery-only. Финальный reviewer env_core_reviewer C0/I0/M0.

Доказательства: `develop/reports/prod-reconciliation-20261015/report.md`,
`reconciliation.json`, `observation-after.json`, `review-final.md`. Для будущего
ARM нужна свежая сверка непосредственно перед активацией и согласованные лимиты.
Старый preparation snapshot до сверки не является текущим recovery flag счёта.

## ENV7 / PT7 — четыре финансовых лимита и GOLD ARM (16.10, DONE)

Пользователь поручил установить четыре значения по прежней политике после пояснения
о сохранении caps вместе с ARM. Ранее утверждённая формула пересчитана по свежим
данным PROD; отдельные P3 caps в TEST отсутствуют, Sandbox суммы не переносились.
В 12:05:36 МСК штатные fresh CANARY reconcile→ARM сохранили:
max_order_notional=5001.1 RUB, max_exposure=5052.3 RUB (CANARY_UID_GROSS GOLD),
max_outstanding_orders=1, max_daily_loss=252.6040 RUB (5% сегодняшней базы5052.08).
GOLD UID8ab18c51-1f14-4c24-9c05-279bc6548821; expiry17.10 00:00МСК.

Account revision10→12/arm12, ARMED/recovery_only=false/effective_can_submit=true,
denial[]. Повторное чтение12:06:34МСК после reconciliation TTL подтвердило caps,
допуск, прежний owner/gen5 и baseline16Oct, автоматически принятую в00:00МСК.
Worker61018COMPLETED/OK, outbox18pending свежих/0failed/0retry. Новых исполненных
сделок в bounded operations пока нет; persisted order facts не читались через
отсутствующий bounded endpoint, полный торговый цикл не заявляется.
Preparation RECONCILIATION_EXPIRED после ARM не отменяет account admission.

Два прежних read-прохода остановились до marker/финансовых POST. Ops helper
рассчитывает caps внутри source TTL до cleanup; Core guards не изменены.
21 ops и14 risk synthetic tests PASS, сверка артефактов16/16PASS;
независимый env_core_reviewer pre-execution и final C0/I0/M0.
Proof: `develop/reports/prod-limits-arm-20261016/{report.md,activation.json,observation-after.json,verification.json,review-final.md}`.
Ручные брокерские заявки, Resume, изменения стратегии, deploy/restarts/DB edits
не выполнялись. Multi-UID, auto ARM и Telegram остаются отдельными задачами.
