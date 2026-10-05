# Актуальный план развития

Актуализировано 2026-10-04 по текущему checkout и сохранённым материалам.
Это единственный индекс текущей очереди. [Фаза 0](phase-0-trading-service-refactor.md)
содержит завершённый baseline; [пунктовый аудит](milestone-audit.md)
объясняет судьбу каждого прежнего требования. Предыдущая версия этого файла — `phase-1-implementation-plan-archive-2026-09-09.md` (необязательный локальный архив)
сохранена как история приёмок, а не инструкция к повторному выполнению.

## Дополнение 02.10.2026: VPS, эксплуатация и реальный API

<a id="v1-service-versions"></a>
<a id="v1--версионирование-сервисов-open"></a>
### V1 — версионирование сервисов

Статус 03.10: код DONE, релиз DEPLOYED; отдельная проверка после выкладки HTTP/API PASS.
Проверка актуальности до реализации: metadata0.2/Core и O1.1 уже существуют;
собственная UI версия, Worker/Analytics observations и build gate отсутствовали.
Root и `milestone_relevance_architect` подтвердили актуальность полного V1,
зависимости/DoD уточнены в [плане](superpowers/plans/v1-service-versions.md).
V1 выбран как самостоятельная локальная кодовая веха; O1/S1/M1 не закрываются.

Реализовано: общая Python версия0.2 с совместимым Core alias; собственная frontend
версия0.2 видна при API error; Worker heartbeat metadata и Core volatile TTL30s;
Analytics private health/OpenAPI и bounded probe; additive diagnostics service_versions;
сборочные Python/npm gates и полный remote preflight guard. Неизвестные/устаревшие
версии — UNKNOWN, готовы ли торговые сервисы этим не подтверждается. Python/npm
версии могут различаться. Правила выпуска — [development](development.md#версии-сервисов).

Авторы: `hierarchy_profile_senior` (senior backend) и `relevance_rules_junior`
(junior frontend); root декомпозировал/ACK/принял фактические пакеты. Независимый
`milestone_relevance_architect`: frontend CODE PASS; backend после исправления
Important (AST override/rebinding bypass) CODE PASS, C/I/M0. Root final nonPG1460PASS/0skip/17warnings и frontend142/type/build PASS,
Ruff/Black/releaseguard/diff PASS; итоговый CODE/DOC/INTEGRATION review PASS, C/I/M0.
Первые1450/143 receipts до guardfix сохранены отдельно. Доказательства:
develop/reports/v1-service-versions-20261003/. Это результаты локальной приёмки.
Native build, working migration и live HTTP/API приняты отдельными доказательствами
выкладки 03.10 ниже; TRADE не разрешён.

### O1.1 — диагностика Core (код DONE, DEPLOYED 03.10)

По поручению владельца основной агент координирует четыре роли из `.agents/`.
Выбран первый самостоятельный этап O1: authenticated
`GET /api/diagnostics/status` и ручная страница `/diagnostics`. Core readiness
и текущий TEST/PROD/access mode отображаются отдельно; пока не измеряемые
Worker/broker/Analytics/market/outbox/portfolio/strategy имеют UNKNOWN.
Готовый Core не выдаёт общий OK. Это кодовая веха с локальной приёмкой;
полные O1/O2/S1/M1 и допуск PROD TRADE сохраняются открытыми.

Контракт и задачи: [design](superpowers/specs/o1-core-diagnostics-design.md),
[план](superpowers/plans/o1-core-diagnostics.md). Контроль ролей и контекста:
[договорённости оркестрации](agent-orchestration.md). Локальная приёмка до
разрешённой выкладки 03.10: Root66API/usecase
и139frontend tests PASS, typecheck/build/Ruff/Black/diff0; независимый
`/root/o1_reviewer` CODE/DOC PASS, существенных замечаний нет.
Доказательства: develop/reports/o1-status-20261003/.

### O1.2 — цикл управления Worker и доставка событий (DEPLOYED)

04.10 пользователь выбрал совместно O1.2 и M1.1 для первого patch. Проверка
актуальности root/`o12_relevance_architect`: `/diagnostics` и navigation уже
существуют, heartbeat версии не измеряет control progress/outbox. Additive
наблюдения через existing heartbeat, Core atomic volatile TTL и UI расширение
реализовали `o12_backend_senior` (sol/medium) и `o12_frontend_junior` (luna/low).
Независимый `o12_relevance_architect` проверил source/контракты и перепроверил
два исправленных сбоя общей регрессии; открытых C/I/M нет. Root лично проверил
diff и сохранность прежнего PROD fix: полный backend без PG1512 PASS,
frontend158 PASS/typecheck/build, Ruff/Black22files/release guard/diff PASS.
Worker control и outbox отдельно от версии; отсутствующие/устаревшие данные
UNKNOWN, ошибки/остановившийся прогресс DEGRADED. Общая торговая готовность
не подтверждается. Доказательства `develop/reports/o12-worker-diagnostics-20261003/`
и `develop/reports/patch-o12-m1-20261003/`. Frontend сначала реализован до
исполнимого RED; отдельное baseline9FAIL→GREEN27 доказательство не выдаётся
за строгую исходную TDD хронологию.
[Контракт/задачи/DoD](superpowers/plans/o1-core-diagnostics.md#o12-control-loop-и-outbox).
Код развёрнут 04.10 в f29472f; свежие наблюдения показывают CONTROL_PROGRESS/
COMPLETED и растущий счётчик в обоих контурах. Полная O1 остаётся открытой;
read-only PROD bootstrap описан в снимке выкладки 04.10 ниже.

### PROD: незавершённый импорт позиции и недоступный Resume

03.10: исправление отображения подготовлено локально, завершение импорта в PROD
остаётся OPEN. Fresh GET 18:00 UTC подтвердил READ_ONLY и один HOLD/sequence0;
отдельный READ ONLY aggregate подтвердил BOOTSTRAPPING, полный исходный snapshot
и отсутствие position cycle. Worker в диагностике UNKNOWN/NOT_OBSERVED.
Брокерские позиции и подтверждённые автоматы Core — разные источники состояния.

Backend передаёт additive `bootstrap_pending`, frontend показывает ожидание
первичной сверки и «—» вместо неподтверждённых финансовых значений в списке и
карточке. Объяснение блокировки Resume доступно рядом с кнопкой. READ_ONLY guards
сохранены; первоначальный bootstrap завершается Worker без команды Resume.
TEST TRADE возобновление обычного HOLD и незавершённого bootstrap сохранено.

Авторы `prod_resume_backend`/`prod_resume_frontend`, независимый
`prod_resume_architect`: FINAL CODE/DOC/INTEGRATION PASS C/I/M0. Root лично прочёл diff; 12 backend
интеграционных и 18 frontend проверок, typecheck/diff PASS. Приёмка документов
завершена; итоговый receipt — `root-final-acceptance.json`. Доказательства:
`develop/reports/prod-resume-20261003/`.

Пользователь повторил требование функционально завершить импорт. Изолированная
composed PROD проверка подтвердила штатный bootstrap без Analytics, scoped claim,
lost ACK/restart/replay и отсутствие заявок; root fresh 1 PASS, независимое review
C0/I0/M0. Metadata 18:41 UTC подтвердили approved installed Worker be84ed4,
Core/PG healthy, существующий PROD volume и около 776MiB доступной RAM.

Адресное восстановление проверенным **уже установленным** Worker
READ_ONLY/strategy=false выполнено до запуска: native preflight и start PASS.
Отдельная ограниченная verify не подтвердила bootstrap и остановила только новый
PROD Worker, сохранив volume. Fresh HTTPS 19:46 UTC: один HOLD/sequence0,
TEST6 IN_WORK, broker reads работают. Функциональный результат остаётся OPEN.
Read-only metadata 20:01 UTC: OOM=false/restart0/exit0, ошибки рабочего цикла.
Structured counters 20:08 UTC подтвердили 40 `ValidationError` при успешных
connection/commands HTTP200. В установленном Core внутренний DTO теряет
`account_id`, `environment`, `access_mode`; Worker отклоняет PROD-подключение
до heartbeat. Минимальный локальный fix передаёт все три поля; real HTTP
roundtrip проверен для PROD READ_ONLY и TEST READ_ONLY/TRADE. Root fresh29 PASS,
Ruff/diff PASS; независимый source/contract review C/I/M0. Код LOCAL DONE,
NOT DEPLOYED; повторного запуска нет. Независимый архитектор принял scope
операции по повторному functional request; это не включение торговли.
Read-only токен запрещает торговые поручения; режим приложения и PROD TRADE
hardguard отдельно сохраняют запрет. Замена токена не является допуском торговли.
План и доказательства: `develop/reports/prod-bootstrap-20261003/`.
На момент 03.10 новая версия и UI fix не были развёрнуты. Владелец закоммитил
пакет и дал deployment GO 04.10; свежий результат ниже. P3/P4/P5 торговый допуск открыт.

### Выкладка 04.10: 0.2.0, f29472f и отдельная проверка

Commit владельца `f29472f405542ba172af4401f81d192246e04f43` установлен по GO
«Запускаем деплой» в 12:42:22 UTC. Release `vps-20261004-f29472f`, оба current
links переключены. 9 running контейнеров; TEST/PROD Worker READ_ONLY,
strategy=false. Shared PG/Worker volumes/caps сохранены, schema0003 уже актуальна:
миграций и dump в этом patch нет. Собраны5 Linux images до остановки, source/
imports/UID/dist hashes приняты root и независимым архитектором.

HTTPS/assets, login/logout/foreign-cookie401, accounts/portfolio/automations
проверены отдельно после delivery. Каталог FUND/RUB nonselected вернул82/82
элемента, account1/1/errors0. PROD read-only bootstrap завершился штатно:
pendingfalse/seq6/quartet4/IN_WORKrev2, Broker/Core/Worker qty/price/currency
совпадают, orders/executions/intents/alloc0. Это не допуск торговли.

Первая strict postverify FAILED на TEST instant outbox0/cache-sequence equality:
PENDING8→CLEAR0, failed0, progress253→271, финансовые checks истинны.
Исходный FAILED сохранён. Single high-water followup13:07:57–13:08:09 UTC PASS:
все6 TEST Core sequence>=captured Worker cutoff, old-prefix absent,
financial/scope/no-orders parity сохранена. Captured pending cohort пуст: kinds/
UUID первоначальных8 событий не доказаны. Поздние8 pending вышеcutoff допустимы,
failed0; это конечная граница доставки, не обещание вечной idle queue/всей истории.
Core/Worker/API снимки неатомарны, новые READ_ONLY WAIT facts разрешены.
Native runtime9running/noOOM/restarts/exactpins PASS, HEAD/index/source50
не изменены. Итоговое независимое runtime/doc review фиксируется отдельно.
Evidence `develop/reports/deploy-20261004/`, delivery exact SHA
`f588364af1211e0b080e176ef5ba75e65607ebf9a3aed1d2ac6eb4af631399fa`.
Подробный scope/первые operational failures: [runtime](deployment/remote-compose.md).

### Выкладка 03.10: 0.2.0 и отдельная проверка после неё

По commit владельца и явному разрешению развёрнут неизменяемый
`be84ed4f047734ccacfba9b6ccb531af51ba4b94`:
`/opt/moex-sentinel/{current,production-current}` →
`/opt/moex-sentinel/releases/vps-20261003-be84ed4`.
Выкладка завершена 17:10 UTC: Core готов до запуска TEST Worker, TEST6 + PROD2,
общая PostgreSQL с `0003_user_broker_archive`, READ_ONLY/strategy=false.
В env изменён только RELEASE_TAG; прежние persistent volumes сохранены.

По уточнению владельца перед миграцией снят только custom PG16 dump
116246851 bytes с SHA и `pg_restore --list`. Полный paired backup, Worker archive,
restore/rehearsal — NOT_PERFORMED. Перед запуском writers миграция сохранила
cardinality всех 17 таблиц и добавила nullable archive metadata; это не проверка
полного восстановления или byte equality финансовых строк.

Проверка после выкладки завершена отдельным шагом 17:12 UTC: HTTPS/assets/auth/
изоляция сессий, scoped accounts (по одному счёту/errors0), portfolio и trading
summary PASS. GET каталога вернул 2460/2462 элементов TEST/PROD; это размер ответа.
Core обеих сред0.2.0, TEST Worker/Analytics OBSERVED0.2.0; PROD WorkerUNKNOWN,
AnalyticsNOT_CONFIGURED. Общий UNKNOWN не означает готовность торговли.
Подтверждение running/images/caps/mounts/networks/env — свежий снимок17:14 UTC.
Независимые post-deploy READ_ONLY приёмка и итоговая проверка документов
`milestone_relevance_architect` — PASS, C/I/M0; итоговый статус DEPLOYED + VERIFIED
в указанной области. Root принял фактические изменения и доказательства.
Доказательства: `develop/reports/vps-release-20261003/root-native-receipts.json`,
`root-archive-timing.json`, `root-final-acceptance.json`, `postdeploy-review.md` и
`final-doc-review.md`. Снимки этапов относятся
к разным моментам: после HTTP, в17:13, архивирована одна отключённая запись;
root не выполнял DELETE/PUT, actor не исследован. Её строка и история сохранены.
Browser click/полная positions/orders/recovery/capacity и24ч/7сут не заявляются.

Процесс выпуска по указанию владельца: сборка до остановки приложения;
выкладка с обязательными migration/readiness/dependency/fail-stop gates;
расширенные проверки HTTP/API/auth/статусов и итоговое review — отдельным шагом
после переключения. Полные тесты исходников во время downtime не повторять.
Исправлены две ошибки упаковки публичных файлов (каталоги0700 и nginx.conf0600)
с сохранением file bytes, original USER и runtime configuration образов.
В следующие доработки входит сокращение ручной выкладки: корректные права
артефактов и отдельная команда post-deploy verification. Полные O1/O2/S1/M1,
длительное наблюдение, PROD Worker и TRADE остаются OPEN/NOT_AUTHORIZED.

### История приёмки 02.10 (не текущий runtime)

Релиз `1b410a9` на VPS выполнен и принят 02.10: TEST и initial PROD READ_ONLY.
Токен введён владельцем через UI; PROD счета/сводка читаются, TRADE отключён.

По commit владельца и явной отмашке 02.10 развёрнут неизменяемый master
`1b410a9`: TEST443 — шесть сервисов, initial PROD8443 — backend/frontend.
Оба Core используют одну фактическую PostgreSQL/одну database; единственный
collector работает в TEST и настроен на оба контура. READ_ONLY и strategy=false;
PROD Worker/TRADE не запущены. Старый отдельный PROD PostgreSQL остановлен,
его volumes сохранены.

Code/integration A/B/C — PASS; изолированный backend **1456 passed, 0 skipped**,
frontend **119 passed**, typecheck/build exit0; PG16 paired fixtures **2 passed,
0 skipped**. Рабочая парная backup/restore приёмка принята составным доказательством:
точные schema/данные/sequences и 13 эквивалентных CHECK, проверенных PostgreSQL
parser roundtrip. Исходный literal FAILED receipt сохранён, не заменён PASS.
Бизнес-сверка PASS: старые 32308 decisions сохранены, новые только WAIT при
отключённой стратегии; identity/execution поля шести cycles и шести lots сохранены;
valuation поля обновлялись.
HTTP **41 checks PASS**, публичная browser TLS/отрисовка PASS. TEST SDK:
2460 RUB инструментов, selected=0, один счёт, семь брокерских позиций;
шесть контролируемых Core-позиций подтверждены отдельно.

Повторная графовая проверка 16:55 UTC: SQLite integrity OK, FK violations=0, frontier behind=0,
Core prefix продвинулся, pending=48 без изменения; STARTED/RECONCILED=160104
стабильны в Worker и Core, collector свежий, ошибок нет. Три RAM замера:
cgroup 735–787 MiB, working set 518–609 MiB; swap вырос после промежуточных
restore репетиций. Это ограниченные замеры, не 24-часовая приёмка.
Независимое initial READ_ONLY operational acceptance — **PASS** (C), по final
inventory/deployed receipt 16:57 UTC: TEST6+PROD2, одна Core PG/БД, короткие
observations. Длительная приёмка остаётся открытой.
02.10 в21:05 MSK принято исправление настроек: токен введён владельцем через UI,
создано новое отдельное immutable подключение TINVEST PROD. Scoped accounts и
общая summary: HTTP200, счёт1/errors0, выбранный API ID совпал, portfolio/free cash
конечны в RUB; runtime READ_ONLY. Старое уже отключённое подключение, его identity,
полный каталог4335 и sync1 сохранены. PROD приёмка частичная: сверка positions/orders,
persistent collector/portfolio, bootstrap/capacity/admission, PROD Worker/TRADE,
repair и 24ч/7сут остаются открытыми.
Диагностический bugfix (шесть файлов): CODE PASS C/root, backend80 и frontend3,
typecheck/Ruff/Black. На момент приёмки02.10 он ещё не был закоммичен/развёрнут.
03.10 delta и archive/schema0003 вошли в be84ed4 и выложены по разрешению
владельца. Проверки релиза1456 не заменяют приёмку этого delta.
Завершён [анализ оркестрации и Worker storage](audits/orchestration-and-worker-storage.md):
Worker оркестрирует исполнение, его durable состояние не заменяется чтением
брокерского портфеля; перенос Worker/Redis/RabbitMQ ещё не выбраны.
Отдельный dedup audit реализован и интегрирован, независимое ревью81 PASS,
root44 PASS; deployment выполнен, измерение уменьшения роста ещё открыто.

Пользователь добавил новую очередь; прежний performance backlog ниже сохранён.
Каталог и шесть позиций Sandbox получены из API, Worker завершил bootstrap.
Повторная read-only проверка02.10: SSH/sudo работают; running strategy=false,
broker/Core/Worker qty/avg и scope сверены, intent/order/execution отсутствуют,
12 событий доставлены с ACK, frontend r2/каталог/позиции видны в браузере.
Финальный browser прогон exit0: logout204, session401, ошибок нет.
Эта browser/runtime проверка относилась к прежнему выпуску. Начальная приёмка
нового выпуска завершена; длительное наблюдение роста остаётся открытым.
Storage baseline измерен и независимо
перепроверен; S1 ещё требует24ч/7суток. Доказательства:
`develop/reports/vps-audit-20261002/`. Предыдущая auto-review ошибка403 не повторилась.

| Порядок | Веха | Условие готовности |
|---|---|---|
| 1 | Sandbox: initial приёмка PASS | Длительное наблюдение далее O1/S1 |
| 2 | O1/S1: диагностика, ёмкость и M1 оптимизация ОЗУ — OPEN | Статус/audit доступны оператору; замеры24ч и подтверждение7дней дают диапазон хранения30/180дней; M1 требует измеримого before/after и независимого review |
| 3 | PROD P1/P2/shared-DB/dedup: code/integration PASS | Одна Core PG/БД, scope guards/collector/Compose проверены; backend 1456/0skip, frontend 119/type/build, isolated paired fixtures 2/0skip. Deployment, рабочая парная backup/restore и ограниченные capacity/runtime проверки выполнены; initial operational acceptance PASS; PROD accounts/summary приняты; остальная PROD приёмка и длительное наблюдение открыты |
| 4 | O2/PROD P3/P4: готовность | Уведомления, лимиты/controlled takeover, восстановление и сверка реального счёта приняты |
| 5 | PROD P5: canary | Отдельный допуск владельца, численные лимиты и один разрешённый UID; расширение отдельно |

### M1 — оптимизация ОЗУ в O1/S1 (OPEN)

Исследовать ограничение объёма каталога и истории в памяти, размеры PostgreSQL
pools по фактической concurrency, профиль SDK и отложенную загрузку его компонентов.
Отдельно исследовать более компактный базовый/runtime Docker-образ и состав
зависимостей: Python уже использует `python:3.12-slim`
(`docker/python-base.Dockerfile`), frontend — Alpine и multistage
(`docker/frontend.Dockerfile`). Меньший образ не гарантирует меньшую ОЗУ;
готовая замена и экономия не заявлены. M1.1 код/ORM resource tests проверены локально:
`ReferenceCatalogRepository.list` загружает ORM партиями, сохраняя полный DTO
tuple/scope/order/selection. Автор `m1_memory_senior` (sol/medium); независимый
`o12_relevance_architect` CODE/METHOD C/I/M0. Resource/семантика/возврат соединения
проверены и в полном root backend1512 PASS. Три fresh paired samples каждого
варианта на1000/20000 строк с одинаковыми seed/result SHA, macOS/SQLite.
Первый root20k peak−20.93%, latency+19.99%; author latency−5.75%.
Один заранее назначенный повтор после остальных проверок:20k peak135.21→106.92MiB
(−20.93%), чтение1.073→0.953s (−11.17%);1000строк75.80→81.28ms (+7.23%).
Оба root receipts сохранены. Из-за разброса и малого числа samples отсутствие
регрессии latency ещё не принято; дальнейший gate — одинаковая PG/API нагрузка.
Linux/Compose/PG/container/image metrics не подтверждены; fullM1 остаётсяOPEN,
экономия VPS не заявляется. Доказательства develop/reports/m1-catalog-20261003/.

Критерий готовности: на одинаковых workload, данных и Compose конфигурации
сравнить before/after cgroup memory.current, working set, PSS, peak, swap и OOM;
compressed/unpacked размер образов измерить отдельно. Финансовые инварианты,
auth и TLS сохраняются; регрессия latency API и доставки outbox не допускается.
Изменения и результаты измерений проходят независимое review.

Документы перехода: [PROD design](superpowers/specs/production-api-design.md),
[план перехода](superpowers/plans/production-api.md),
[наблюдаемость](deployment/observability-plan.md),
[хранение30/180дней](deployment/storage-capacity.md).
Идентификаторы P1–P5 здесь относятся к PROD плану, а не оценкам приоритета
P1/P2 прежнего performance backlog. Код PROD P1/P2 реализован; независимый
`/root/prod_integration_review_retry` — code PASS (150 Python,29 UI).
Наблюдаемость, retention и P3/P4/P5 остаются открытыми. Initial PROD развёрнут
на общей БД; авторизованные PROD accounts/summary приняты, остальные чтения и
сверки остаются открытыми.
SDK1.49.3 AsyncClient с синтетическим неверным токеном дал UNAUTHENTICATED:
подтверждены TLS/gRPC, без авторизации RPC; evidence
`develop/reports/prod-api-20261002/prod-sdk-network.json`.
Владелец ввёл PROD токен через UI; Sandbox token/account/
ledger не копируются. Sandbox обновлён с сохранением env и persistent volumes.
Пользователь разрешил поэтапную реализацию чтение→торговля; реальные
заявки и очистка рабочих фактов требуют отдельного допуска.

## Завершённая веха: 0.9.8 — ограниченная по памяти доставка outbox и корректные повторы

**[x] Принята 28.09.** W1, W2a, W2b и V0 выполнены. Сквозные
PostgreSQL-сценарии, backlog 100/10k/50k, финальная backend-регрессия и
независимое итоговое ревью завершены без существенных замечаний.
[Оценка кодовой базы](audits/codebase-improvement-assessment.md),
[постановка, границы и DoD](milestone-0.9.8-outbox-delivery.md),
результат W1 — `../develop/reports/local-development-coordinator/skill-w1-forward.md` (необязательный локальный архив).

Независимые ревью W2a (`/root/review_w2a`) и W2b (`/root/review_w2b`) —
**PASS**. Frozen compare совпал на 6/6 normal/bootstrap наборах 100/10k/50k.
Bootstrap 50k old/new: peak tracemalloc 108105444/382335 B, время
отбора, медиана 3 повторов, 6366.750/2894.517 ms, SQL 4/5. Итоговые значения
W2b — в selector comparison JSON — `../develop/reports/0.9.8-w2/selector-comparison-v2.json` (необязательный локальный архив).
Детерминированный предел при limit=10:
metadata 24, Session identity map 10, quartet payload 4, DTO не более limit+3;
буфер драйвера отдельно — до 128 строк. Normal selector не демонстрирует общего
ускорения: повторные медианы old/new — 24.580/25.953 ms при 10k и
65.517/74.404 ms при 50k, p95 шумный. W2a baseline — `../develop/reports/0.9.8-w2/BASELINE-IN-PROGRESS.md` (необязательный локальный архив)
— историческая характеристика, зафиксированная до W2b; повтор normal-замеров —
в normal perf repeat — `../develop/reports/0.9.8-w2/normal-perf-repeat.md` (необязательный локальный архив).

В V0 сквозные bootstrap, mixed selective ACK и retry-before-due проверены через
реальный HTTP и отдельный Core PostgreSQL; backlog 100/10k/50k завершился без
потерь. Selector drain old/current совпал на 18 парных запусках первого seed
и 9 persisted наборах второго seed по количеству, остатку и порядку.
Adversarial cap при многих ранних quartet исправлен после RED; подсчитаны
строки приложения в bootstrap metadata scan, включая 1 274 970 на many 50k.
Финальный полный backend прогон: 1334 passed, 0 skipped, 17 warnings, exit 0;
Ruff, Black и `git diff --check` прошли. Повторное независимое интеграционное
ревью `/root/review_v0_integration` — PASS после исправления трёх существенных
замечаний. Подробности и ограничения —
в V0 отчёте — `../develop/reports/0.9.8-v0/README.md` (необязательный локальный архив). R0 также открыт.

Выбраны W1/W2 — два изменения одного пути доставки Worker. После долгой HTTP-попытки
durable retry должен отсчитываться от свежих часов; подбор batch должен удерживать
ограниченное окно метаданных/payload вместо полной материализации PENDING.
Сначала характеристические тесты и baseline 100/10 000/50 000 фактов, затем
изменение и сравнение на тех же данных. Сохраняются точный batch, bootstrap quartet,
deadline, blocked prefix, selective ACK и восстановление после потери ответа.
Целевой результат — корректная пауза и доказанный предел памяти; процент ускорения
полной цепочки заранее не заявляется. Перенос SQLite и другие оптимизации сюда не входят.

Перед оперативным закрытием recovery-исправления 19.09 нужна свежая проверка
runtime: сохранённый `after-deployment.json` подтверждает deployed source и
healthy на 19.09, но не новые решения и устойчивость. На 28.09 Docker daemon
доступен для отдельной тестовой PostgreSQL; рабочие сервисы в рамках V0 не
проверялись, поэтому текущий статус торговли неизвестен. Пакеты реализации и
проверки сведены в [пул задач](phase-1-implementation-plan.md).

## Текущая очередь после 0.9.8

Это кандидаты с конкретной областью проверки; приёмка отдельных пакетов указана ниже.
ID, доказательства, риски и тесты приведены в оценке выше. Порядок строк задаёт
ближайшую очередь; условные оптимизации выбираются только после профиля.
F1 уже реализована в HEAD; 04.10 полный frontend повторно проверен с доступным
npm (204 PASS). В очередь реализации F1 повторно не включаем.

| Очередь | Объём | Условие и ожидаемый результат |
| --- | --- | --- |
| A1, P1 — LOCAL DONE | Неблокирующий HTTP-контур Core | Целый sync usecase/UoW вне event loop, ограничение потоков, конкурентные CAS/replay/rollback и корректная атрибуция метрик. |
| 2 — F2/F3, P2 | Актуальность UI-запросов и route-контекста | Поздние ответы не меняют текущие данные/формы; отображение и действия используют один актуальный инструмент/счёт. |
| 3 — W3/C5, P2 | Независимые задачи устойчивости | Отдельно tracking SQLite offload; отдельно ограничение cursor pagination/shutdown collector. Не объединять транзакционные границы сервисов. |
| C1/C2/C3, P2 — LOCAL DONE | Точечные Core SQL/read-model изменения | Сначала контракт и SQL budget каждого пути; lineage reads, N+1 и order/cycle RETURNING принимаются независимо. |
| C4/W4, P2/P3 — LOCAL DONE | Summary и сопровождение ledger | Summary — после профиля истории; общие session-bound LIFO helpers — с эквивалентностью двух входов и атомарностью. |
| Условно — W5/F4, P3 | Analytics metrics / детали API | CPU-профиль до кэша; профиль независимых I/O-веток до параллельной загрузки. Сохранить TTL и partial errors. |

04.10 пользователь выбрал совместный пакет **A1 + C1–C4 + W4**. До реализации
root, `a1_c14_w4_architect`, `c14_core_senior` и backend senior `next_ui_senior`
перепроверили текущие потоки и зависимые контракты. 04.10 short design явно подтверждён
пользователем; пакет реализуется с sole writers в отдельном worktree. Ledger и доказательства:
`develop/reports/core-performance-20261004/`. Прежний принятый patch сохраняется.

Уточнения актуальности: A1 охватывает sync-only HTTP и DB preflight внутри
mixed async flows; whole operation/UoW остаётся в одном worker, SDK/async lock —
в loop. Для строгого cancellation/drain нужен app-owned bounded executor, а
не перенос Session между потоками. C2 N+1 есть также в service environment
filter: нужен существующий broker batch read вместе с repository JOIN.
C1 error priority сохранить внутри назначенных validation targets, не менять
соседние typed error ветви. C3 использует существующий RETURNING pattern.
C4 требует профиля длинной истории до product batching трёх окон summary.
W4 unused `_ledger` уже удалён из order tracking; остаётся только duplication
session-bound opening/LIFO, без изменения финансовых формул/transaction/outbox.

Для новых gates поднята отдельная локальная PostgreSQL16 (loopback/tmpfs, без
рабочих volumes). Fresh исходный nonPG backend1512PASS; первый PG baseline
100PASS/1FAIL/2SKIP: stale health test ожидал0.1.0 вместо принятой0.2.0,
dump/restore tools host major не совпал с16. Первые логи сохранены.
Отдельный maintenance принят: health spec использует canonical version без
ослабления503/schema/no-leak; реальные pg_dump/pg_restore16.15 через локальные
изолированные wrappers. Автор next_ui_senior (backend senior sol/medium),
независимый a1_c14_w4_architect (sol/high) CODE/CONTRACT/METHOD PASS C0/I0/M0.
После root интеграции fresh полный PG baseline103PASS/0FAIL/0SKIP,65.92s;
nonPG1512PASS/101.15s. Это исходное состояние, не приёмка новых A1/C/W4.
A1/C1–C4/W4 локально интегрированы после независимого CODE/CONTRACT/SOLID/
TEST/METHOD review (C0/I0/M0). C1 сокращает existence/lineage reads с прежним
порядком ошибок; C3 использует full UPDATE RETURNING и обновляет stale identity
при rejected cycle update. C2 normal repository list — 1 SELECT, normal service
scope — 2 SELECT при 1 и 50 автоматах; неполный/чужой instrument scope явно
отклоняется. W4 сохраняет численные результаты, атомарность и outbox/replay двух
entrypoints. A1 переносит whole sync Session/UoW в bounded app-owned pool cap4;
отмена HTTP не освобождает слот незавершённого задания, shutdown ждёт drain до
engine.dispose. Async SDK/locks/cache остаются в loop; вне HTTP прежний режим
сохранён. Граница исполнения записана в AGENT_BRIEF.md.

C4 сначала измерен на SQLite/PG: 5 synthetic history profiles, ALL/TEST/PROD,
max86405 history rows одновременно. Первая paired версия показала регрессию PG
long ALL/TEST; raw receipts и неудачный NOT MATERIALIZED diagnostic сохранены.
Actual EXPLAIN обосновал исправление: PG GROUP/ORDER/LIMIT в одном SELECT вновь
останавливается по индексу; SQLite CTE и старый singleton сохранены. Второй paired
прогон: все30 SQL/median/p95/output gates PASS. Whole-view single-currency reads
8/9/10→4, mixed6/7 вместо14/15; empty scope2 остаётся2. PG long ALL median91.170→
47.128ms, p95100.564→50.283ms. Это ограниченный локальный synthetic профиль;
производственная нагрузка не измерена. Фактически32 seed операции после первого
прогона и диагностики; owned schemas/files очищены, рабочие volumes не затронуты.

A1 первый HTTP synthetic replay замер отрицательный и сохранён; точные bytes
первого measured script не были зафиксированы. После исправления одинакового
concurrent warmup для old/new exact script архивирован до замера; одна fixed pair
89.38→100.92req/s, p9559.50→50.98ms. Общего production ускорения не заявляем.

**A1 + C1–C4 + W4: DEPLOYED 04.10 в f29472f.**
Независимый a1_c14_w4_architect: итоговое CODE/CONTRACT/SOLID/TEST/INTEGRATION/
DOC/RECEIPT PASS, C0/I0/M0; все обязательные локальные gates выполнены.
Fresh root nonPG1674PASS/0FAIL/0SKIP, PostgreSQL187PASS/0FAIL/0SKIP,
frontend204PASS/26files, typecheck/build, aux11PASS; Ruff/Black72files/diff PASS.
Первые full nonPG1671PASS/2FAIL и quiet1672PASS/1FAIL сохранены: старые gateway
тесты предполагали гарантированный wall-time interval меньше10ms. Runtime/
recovery не менялись. Один original diagnostic trace прошёл, исходный сбой не
воспроизведён с trace; отдельный synthetic12ms pause подтвердил корректное
переподключение после10ms и ошибочную предпосылку теста. CPU-причина не доказана.
Исправлен только spec: контролируемое время отдельного loop, реальные ingest/
cancel acknowledgements, прежние10ms/5ms/10ms пороги, equality check и прежний
cancellation-resistant negative. Scoped45PASS и3 negative probes; independent
review C0/I0/M0. После exact integration свежий полный nonPG1674PASS/99.13s.
Format-only maintenance bootstrap spec имеет полную AST parity/scoped10PASS.
Production и PG-suite исходники после PG187PASS не менялись; последние corrections
касаются только nonPG specs. Все первые неблагоприятные доказательства сохранены.

Root exact50 source paths сверены с reviewed worktree; baseline683 paths:
648 сохранены,35 согласованных изменений, неожиданных0; HEAD/index неизменны.
Owned synthetic PG schemas/restore databases очищены, test-контейнер остановлен
для освобождения ОЗУ; рабочие volumes/VPS не затрагивались. Итоговые evidence/
review: develop/reports/core-performance-20261004/{final-report.md,
root-final-acceptance.json,root-integration-independent-review.md}.
Новых API/dependencies/миграций/Worker storage переносов/PROD TRADE нет.
Полные M1/O1/O2/PROD P3–P5 остаются открытыми, W3/C5 и условные W5/F4 не входят
в выбранный пакет. Пользователь коммитит сам; deployment — отдельный GO после
локальной приёмки.

04.10 актуальность F2/F3 перепроверена root, `next_ui_senior` и независимым
`o12_relevance_architect` по текущим исходникам. Пользователь выбрал оба
ограниченных пакета перед тем же patch: F3.1 — карточки инструмента/позиции/счёта
при смене route; F2.1 — поздние чтения каталога и операций при смене фильтров/лимита.
Всего пять существующих views и пять specs, без изменения API/финансовых команд,
auth, БД и зависимостей. **F2.1/F3.1 DEPLOYED 04.10 в f29472f.** Senior
`next_ui_senior` реализовал карточки, `f2_frontend_senior` — каталог и принял
операции от junior `f2_operations_junior`. До изменения компонентов выполнены
RED-прогоны; регрессии проверены через настоящий RouterView и deferred ответы.
Root интегрировал exact frozen файлы после независимого
`f23_integration_reviewer` CODE/CONTRACT/TEST COVERAGE PASS, C0/I0/M0.
Свежий полный frontend:204 PASS/26files; typecheck/build/architecture1PASS/diff
успешны. SHA перенесённых10файлов совпадают с проверенным worktree, прежний patch
и Git index сохранены. READ_ONLY, прежний bootstrap UI, timer cleanup, partial
errors/cache и итог отправленных mutations сохранены; старый callback не меняет
новую форму и не вызывает stale redirect. Query того же инструмента сохраняет
draft; команды используют ID строки каталога, свечи — внешний UID.
Broker editor pending save/drafts не входит. Полная строка backlog F2/F3 не
закрывается частичными пакетами. Relevance/briefs/ledger/proofs:
`develop/reports/next-patch-20261004/{final-report.md,root-final-acceptance.json,
independent-review.md}`. Ограничения: API в frontend-тестах подменён; native VPS
и реальный PROD bootstrap этим пакетом не проверялись. В operations unmount test
проверяется видимость нового экземпляра; guard старого экземпляра подтверждён
ревью кода. F3 A→B→A подтверждён монотонным counter review, отдельного теста нет.
Backend source не менялся: прежний full backend1512 PASS без PostgreSQL остаётся
доказательством предыдущего этапа, а не новым прогоном04.10. Владелец коммитит;
выкладка и postverification — отдельные шаги после GO.

Оценка 10.09 описывала эти проблемы как будущую работу. На 26.09 F1 уже есть
в HEAD; остальные строки остаются кандидатами, кроме локально принятых выше
ограниченных пакетов F2.1/F3.1.

## Завершённая веха: 0.9.5 — обработка недоступности песочницы

**[x] Завершена.** [Приёмка](milestone-0.9.5-acceptance.md),
[полная постановка и DoD](milestone-0.9.5-sandbox-resilience.md).
Проверены безопасное ожидание, ограниченные повторы, отсутствие конкурирующих
переподключений и восстановление без повторных заявок. По последнему уточнению
пользователя после первоначальной попытки допускаются до пяти повторов с паузами
1, 3, 5, 7 и 9 секунд; количество задаёт `SANDBOX_RETRY_LIMIT` (по умолчанию 5).
На момент приёмки 0.9.5 после исчерпания лимита Core gateway прекращал повторы
до изменения конфигурации источника или перезапуска. Обнаруженный 19.09 дефект
исправлен в рабочем дереве и развёрнут по сохранённому hash probe: после короткого
лимита transient ошибки проходят через cooldown 60 секунд и повторяются; текущая
оперативная приёмка этого изменения ещё открыта. Заблокированному Worker нужен
перезапуск runtime/процесса:
изменение конфигурации в Core само по себе не пересоздаёт Worker bundle. Свежесть определяется
временем данных; строгий TTL сохраняется, здоровые инструменты продолжают работу.
Существующие intent/outbox и пользовательский HOLD сохраняют свою семантику.

Внешняя причина недоступности песочницы не является предметом обязательного
расследования и условием приёмки. Изолированные сценарии отказа и восстановления
дают основное доказательство; краткая runtime-проверка не заменяет длительный SLA.
Независимое кросс-ревью по SOLID, чистоте кода и тестам пройдено; результаты
и ограничения проверок приведены в приёмке.

<a id="следующая-веха-094--профиль-доставки-фактов-на-core-postgresql"></a>
## Завершённая веха: 0.9.4 — профиль доставки фактов на Core PostgreSQL

**[x] Завершена.** [Приёмка и измерения](milestone-0.9.4-acceptance.md),
[план выполнения](../develop/benchmarks/README.md).
Измерен реальный путь Worker SQLite → HTTP ingress → Core PostgreSQL в отдельном
тестовом контуре. Benchmark
0.9.3 использует SQLite для Core и не доказывает необходимость смены хранилища
или конкретной оптимизации.

Выполненные критерии первого этапа:

1. Одна воспроизводимая команда поднимает/использует отдельный тестовый PostgreSQL,
   рабочие БД и volumes не затрагиваются; брокер и рынок синтетические.
2. Профиль 6/20/50 инструментов явно разделяет решение, ожидание очереди,
   транспорт и Core commit. Публикуются выборка, p50/p95/p99, throughput,
   параметры batch и условия запуска.
3. WAIT-нагрузка и сценарии с исполнениями обозначаются отдельно; измерения
   не переносятся на другой профиль без проверки. Потеря ACK и повтор не создают
   потери либо дубли подтверждённых фактов.
4. Отчёт называет обнаруженные узкие места и ограничения; целевой порог нагрузки
   фиксируется до оценки соответствия. Без заданного порога production SLA не заявляется.
5. Решение об оптимизации принимается по профилю. Изменения, если они нужны,
   получают отдельный конкретный объём, регрессию и независимое кросс-ревью.

Это уточнение существующего 0.9.4, а не обещание ускорения без измерений.

<a id="следующая-веха-096--сокращение-повторных-чтений-core"></a>
## Завершённая веха: 0.9.6 — сокращение повторных чтений Core

**[x] Завершена.** [Постановка](milestone-0.9.6-core-sql-roundtrips.md),
[приёмка и сравнение](milestone-0.9.6-acceptance.md).
UPDATE RETURNING заменил повторный SELECT после успешного CAS. Сохранены
conflict/revision/sequence, актуальность DTO/UTC, rollback и exact replay;
диагностика конфликтов больше не зависит от устаревшего identity map.
Повторный профиль 6/20/50 дал 102/103/106 SQL-вызовов на WAIT-решение вместо
111/112/115 — ровно один запрос меньше на принятый факт. В этом прогоне drain p95
снизился на 8–16%; ограничения сравнения приведены в приёмке. Полная регрессия:
1125 passed; независимое кросс-ревью пройдено, Core обновлён в Compose.

## Завершённая веха: 0.9.7 — объединённое чтение идентичности факта

**[x] Завершена.** [Согласованный объём](milestone-0.9.7-envelope-lookup.md),
[результаты приёмки](milestone-0.9.7-acceptance.md):
один lookup по event/sequence вместо двух, с сохранением replay, приоритета
конфликтов, scope и атомарности группы. Реализация и полная регрессия завершены:
1152 passed; независимое ревью кода и итоговой интеграции пройдено. Профиль подтвердил 93/94/97 SQL
на WAIT-решение вместо 102/103/106. Core обновлён в Compose, healthcheck успешен.
По latency основной профиль 20/50 хуже исторического baseline; ABBA и проба
на 50 000 событий не дают основания заявлять устойчивое ускорение всей цепочки.
Ограничения и контрольные измерения явно приведены в приёмке.

Завершённая веха 0.9.8 и оставшаяся очередь определены выше по отдельному аудиту.
Пакетное чтение envelopes, межфактовый кэш и новая группировка обычных фактов
не включены в неё: каждому изменению нужны своё обоснование и конкретный объём.

## Условный backlog: 0.9.2 — хранилище Worker

**Условно отложено, MAJOR.** Core уже использует PostgreSQL. Worker остаётся на
собственной SQLite с WAL и durable outbox. Возврат к вопросу о переносе допустим
после доказанного ограничения именно Worker storage и оценки восстановления,
задержек и эксплуатационной стоимости. Профиль Core PostgreSQL сам по себе
не обосновывает перенос Worker. Сейчас реализация переноса не запланирована.

## Закрытые и поглощённые пункты

| Объём | Результат и доказательство |
| --- | --- |
| 0.1–0.8 | [Завершённый baseline с кодом, тестами и приёмками](phase-0-trading-service-refactor.md). |
| 0.9.1 | Push входит в 0.5: [подписки SDK](../src/moex_sentinel/adapters/tinvest/streaming.py), глубина стакана 20. |
| 0.9.3 | [Benchmark, UI, bootstrap DTO, исправление лимита WAIT](milestone-0.9.3-acceptance.md). |
| Clean-slate и сводка торговли | [Приёмка запуска и схемы](milestone-acceptance.md); сохранённые snapshot собирает отдельный Core worker. |

Старые результаты тестов и наблюдений относятся к датам приёмок. Актуализация
плана сама по себе не означает повторное прохождение этих проверок.

<a id="16-надёжность-выполнения-и-реакция-жизненного-цикла-crit"></a>
## Связь с прежними задачами надёжности

Исторический пункт 1.6 заменён реализованными контрактами lifecycle/исполнения
в 0.3 и 0.8 и обработкой недоступности в 0.9.5. Архивные аудиты ссылаются
сюда для навигации; они не создают второй параллельный план реализации.

## Правила продолжения

Начинать с текущей вехи, затем переходить к следующей. Не возобновлять удалённые
shadow-миграции, импорт старой истории, отдельный каталог стратегии и повторное
выделение Analytics. Стратегия одна, определяется кодом; дополнительных полей
бюджета нет. Не менять эти решения без нового требования пользователя.

Для каждого изменения соблюдать [правила кросс-ревью](../AGENTS.md), тестировать
БД на отдельном экземпляре, сохранять пользовательские изменения и volumes.
При завершении обновлять этот индекс, соответствующую строку фазы 0 и приёмку;
исторические отчёты не переписывать под новые результаты.
