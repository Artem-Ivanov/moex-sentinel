# Актуальный план развития

Актуализировано 2026-10-20: read-only проверка 20:30–20:40 МСК подтверждает
работу PROD-сервисов и открытый общий допуск. GOLD и SIBN остаются HOLD9 с
активными UNCERTAIN; автоматическая торговля не подтверждена. База20.10 принята
автоматически09:13:59МСК. Свежие доказательства и задачи приведены ниже.

## Дополнение 20.10.2026: приложение доступно, торговля заблокирована UNCERTAIN

Применён `develop/skills/moex-trading-audit/SKILL.md`. Read-only SSH, supported
API и bounded allowlist logs; рабочие SQL/SQLite не читались, состояния автоматов,
настройки, заявки и сервисы не менялись. Core/Analytics/frontend healthy,
Worker running, restart0. Публичные index/JS/CSS отвечают200; HTTP-проверка с
`curl --insecure` не подтверждает доверие сертификату, browser UI не проверялся.

26 GET в17:35:07–17UTC и17:37:28–34UTC успешны. Actual PROD/TRADE/strategy=true;
effective admission=true, ownergen8/rev513, lease обновляется. База20Oct
принята06:13:59UTC (09:13:59МСК), equity5051.38RUB. Сохранённые лимиты:
заявка5001.1RUB, экспозиция5051.94RUB ACCOUNT_GROSS, одновременно1,
дневной убыток252.5970RUB. Исторические DISARMED/recovery_only и сверка16Oct
не являются текущим запретом отправки или свежим списком брокерских заявок.

Оба найденных автомата HOLD9/active UNCERTAIN/no terminal, persisted reason
`Uncertain broker intent could not be reconciled unambiguously`. Последние
принятые решения WAIT/ACTIVE_INTENT относятся к19Oct10:33:33/37МСК и не
подтверждают сегодняшний такт. Broker/Core GOLD18лотов совпадают; SIBN Core0,
брокерская строка отсутствует; Worker ledger/provenance не проверялся.
Iterations175902→176039, outbox0pending/0failed, sequence продвигается — это
работа control/recovery, не доказательство исполнения заявки.

В returned operations401запись(limit500), последняя08Sep, записей20Octнет;
полнота истории неизвестна. Current service запрашивает1page, фильтрует и теряет
next_cursor. Последний bounded Worker log срез содержит SDK NOT_FOUND;
исходные intent/request/submit/fill/ACK ещё не коррелированы. Два наблюдённых
Analytics warning завершились recovery через280/439ms; устойчивый отказ
Analytics и причинная связь с HOLD не доказаны. NO_DECISION_FRAME приHOLD и
Core versions.analytics NOT_CONFIGURED не равны падению Analytics. Логи усечены.

Ближайшая очередь, без новых ручных торговых допусков:

1. **P1 — scoped read-only диагностика исходных intent.** Показать по каждому
   автомату process/request/broker order ID, времена и исход submit, executions,
   terminal proof, sequence/ACK; неизвестное помечать явно. Worker публикует свою
   проекцию, Core сохраняет границы scope. Приёмка: два текущих intent можно
   однозначно проследить; тесты cross-scope/redaction/лимитов/unknown на изолированном
   стенде. Существующие APIs этих данных не дают — это доказанный пробел.
2. **P1, после диагностики — исправить установленную причину и безопасно
   восстановить цикл.** Выбор исправления зависит от доказанного исхода двух
   заявок. Сохранять intent/резерв до terminal proof, не повторять submit при
   NOT_FOUND и не очищать UNCERTAIN вручную. Приёмка: isolated lost ACK, partial
   fills, unknown/NOT_FOUND и replay; ровно одно применение исполнения и корректный
   Core ACK. Request-only lookup закреплён контрактом/тестом: broker-ID fallback
   сам по себе не считается доказанным исправлением.
3. **P2 — проверяемая история операций за интервал.** Поддержать bounded date/cursor
   чтение и completeness/truncation metadata, используя имеющиеся возможности
   адаптера и snapshot collector. Приёмка: несколько страниц, фильтр денежных
   операций, порядок/дедупликация, границы UTC/МСК и ошибки источников; нельзя
   заявлять полноту по числу строк меньше limit.

Локальные report/tasks/evidence: `develop/reports/application-audit-20261020/`.
Collector/report author `audit_collector_author_20261020`, read-only архитектор
`audit_architect_20261020`; итоговая независимая проверка `audit_review_20261020`
фиксируется в final-review.md. Анализ не является разрешением на live recovery
или deployment и не подтверждает новую торговлю.

## Дополнение 19.10.2026: свежий статус торговли — HOLD / UNCERTAIN

Применён сохранённый `develop/skills/moex-trading-audit/SKILL.md`. Supported API
07:47UTC: effective_can_submit=true/denial reasons[], ownergen8/rev512,
lease штатно обновляется. База19.10 принята09:18:46МСК, equity5051.92RUB;
лимиты5001.1/5051.94/1/252.5970RUB сохранены. База и общий допуск не блокируют
новые заявки; recovery_only остаётся provenance, ручной ARM не требуется.

SIBN и GOLD теперь **HOLD9 / active UNCERTAIN без terminal proof**. Persisted
причина обоих: Uncertain broker intent could not be reconciled unambiguously.
Последние принятые Core решения10:33:33/37МСК — WAIT/ACTIVE_INTENT; на момент
чтения их возраст около14минут, это не текущий tick. Вчерашнее IN_WORK/ожидание
buy-window — история. Worker control и broker runtime продвигаются; outbox
4pending/возраст1сек/retries0/failed0 не доказывает зависание. В ограниченных
Worker логах повторяется BROKER_ORDER_NOT_FOUND/NOT_FOUND, но исходные заявки
с этими строками ещё не коррелированы. Причина появления UNCERTAIN не доказана.

Broker/Core GOLD18лотов совпали; SIBN брокерская строка отсутствует, Core0.
Worker lot ledger/provenance не проверялись. Операций19.10 в полученных84строках
API нет, последняя возвращённая08.09; полнота финансовой истории не заявляется.
Core unresolved1 и Worker UNCERTAIN2 — разные сущности. Outstanding0 получен
из historical reconciliation16Oct и не является свежим брокерским доказательством.

Только диагностика, без SQL/SQLite/Resume/clear/repost/изменения ENV/deploy.
Задачи: установить цепочку исходных intent/request→broker result→fills→fact/ACK
по каждому автомату; исправлять только доказанный дефект на изолированном стенде,
до terminal proof сохранять intent/резервы. При нехватке supported данных —
минимальная scoped диагностика, без новых торговых допусков. Локальные отчёт,
задачи и evidence: `develop/reports/trading-status-20261019/`. Collector author
command_sync_author; независимый reviewer trading_resume_review закрыл I1–I3,
collector и финальная сверка evidence/docs C0/I0/M0, receipt final-review.md.

Дополнительная read-only проверка API с VPS19.10, результат11:03МСК:
PROD endpoint корректный, DNS/TCP443 доступны; SDK1.49.3 с собственным CA
проходит проверенный TLSv1.3, GetAccounts55ms и GetPortfolio45ms успешны.
Настроенный счёт найден, reported access_levelFULL_ACCESS. Стандартный CA store
хоста/контейнера не доверяет НУЦ цепочке(code19); SDK использует bundledCA,
поэтому эта hostTLSошибка не объясняет текущие ответы брокера. ICMP не отвечает,
но API по gRPC работает. Source hashes двух actualWorker lookup-модулей совпали.
Безопасные структурные логи уточнили реальный RPC: GetOrderState/NOT_FOUND/50005.
Recovery корректно использует REQUESTID, но не пробует известный exchangeID;
это ограничение, не доказанная причина двух intent. По документации брокера
метод может не видеть поручения старше суток. Возраст/исходныеID/submitрезультат
этих intent ещё не коррелированы. Полный доступ в GetAccounts не подтверждает
принятие торговой заявки. Отчёт/evidence:
`develop/reports/broker-not-found-20261019/report.md`. Runtime/заявки не менялись.

## Дополнение 17–18.10.2026: полнота PROD — DEPLOYED / TECHNICAL ACCEPTANCE PASS

Продолжение 18.10 — **PROD runtime restored / decision API deployed**.
На22:38МСК поддержанные postchecks подтверждают Core
`decision-reasons-f7601c66362b`/imagee11 и прежний Worker1d426/image9d32 без
перезапуска при этом обновлении. Public release link остаётся dd5. Ownergen8/
revision511, lease штатно продлевается, effective=true/denial reasons пусты.
Лимиты5001.1/5051.94/1/252.5970RUB ACCOUNT_GROSS, ENV, база и volume сохранены.
SIBN после одного штатного Resume находится IN_WORK/rev5/grouped; новые события
поступают. GOLD HOLD6/active UNCERTAIN не снимался; brokerGetOrderStateNOT_FOUND50005
не позволяет вручную считать историческую заявку закрытой.

A+B исправления Core работают: реальный Worker получает HTTP200 вместо422 при
чтении исходного submit attempt, persisted hold_reason показывает причину HOLD.
Добавлен bounded operator GET последних уже принятых Core решений:
`/api/trading-automations/{id}/decision-reasons?limit=5` (max20). Только scoped
broker/automation/stage STRATEGY_DECISION_MADE, latest поoccurred_at/event_id,
наружу только event/time/nullable decision/reason и captured_at. Malformed latest
не скрывается. Пакет меняет семь файлов Core; Worker/стратегия/ENV/DDL не меняются.

Фактическое событие SIBN22:32:05МСК — **WAIT / BUY_WINDOW_UNCONFIRMED**.
Стратегия ожидает подтверждённое окно покупки. Этот код объединяет отсутствие
индикаторов и непрошедшую проверку buy_window_confirmed; сам по себе не доказывает
закрытие рынка, конкретный пропуск свечей или недостаток средств. Exact Analytics
window исторического решения операторский API пока не возвращает. Заявку не
форсировали; исполнение новой сделки не входит в подтверждённый результат.

ONECore-only rollout выполнен. Его6-секундный lease-postcheck завершился
CORE_HEALTHY_LEASE_UNAVAILABLE, поэтому PASS самого helper не заявляется.
Отдельные наблюдения22:31 и22:38МСК подтвердили тот же owner, продление lease,
здоровый Core, сохранение Worker/caps и продвижение обычного цикла. UIHTTP200.
Независимое source/OPS/bindings/integration review trading_resume_review:
C0/I0/M0; peer adapter review command_sync_author закрыл lowercaseDockerabsence.
Root96целевых/связанных sourceтестов и19OPSтестов PASS, Black/Ruff PASS.

Планы до декомпозиции находятся в
`develop/.planning/2026-10-18-prod-trading-resume/`: decision-reasons-milestone.md,
decision-reasons-ops-milestone.md; предыдущие handoff/Resume receipts не повторять.
Итоговые локальные доказательства:
`develop/reports/prod-decision-reasons-20261018/acceptance.json`, runtime-final.json,
live-decision-reasons.json, rollout-observation-02.json, image-hash-probe.json.
Техническая веха завершена; историческая неопределённость GOLD и ожидание условий
стратегии остаются явными границами торговой приёмки.

Пользователь подтвердил сохранять настоящий TEST/Sandbox и mocks в tests,
исправлять только реальные PROD-пробелы и выпустить исправленный результат.
Независимый статический [аудит](audits/prod-completeness-20261017.md) выявил
недостаточную контурную изоляцию части operator admission API, неиспользуемый
order TTL/cancel, скрытый permanent Worker block, неподтверждённый API trade
flag и ограниченные gaps alerts/Analytics-to-PROD integration coverage.
Подмен PROD RPC на Sandbox в просмотренной композиции не обнаружено.

Согласован дизайн TTL: отменять остаток после исходного durable deadline,
учитывать реальные fills, удерживать active intent/резерв до terminal proof
и атомарной финализации; UNKNOWN сохраняет HOLD/UNCERTAIN без повторного post.
Обычный подтверждённый cancel не вводит ручной HOLD; следующий fresh strategy
intent проходит новый permit и общие лимиты. Lifecycle никогда не делает Resume.
Письменная [спецификация](superpowers/specs/2026-10-17-prod-completeness-design.md)
принята поручением пользователя18.10 приступить к реализации и затем к деплою.
По независимо проверенному [плану](superpowers/plans/2026-10-18-prod-completeness.md)
реализованы Core isolation/unary availability, Worker TTL/cancel/recovery,
diagnostics/alerts/UI и полный production-composition happy path. Общий прогон
также выявил и закрыл дефект ACCOUNT anchor rotation/закрытого UID; финансовая
история и idempotency сохранены. Independent source/integration/financial и
deployment review завершились C0/I0/M0.

18.10 в 09:02:08 UTC выполнен rollout `vps-20261018-prod-437cc0757b90`: четыре
image Core/Worker/Analytics/UI, clean stop прежнего Worker и единственная
передача owner на generation 6. Health/ping и UI index/assets прошли; Chrome
подтвердил отображение страницы входа. Схема исходников осталась на 0007,
DDL/приватный ENV/численные caps/volumes не менялись, команды HOLD/Resume не отправлялись.
Root Python: 2804 PASS, 2 SKIP только rehearsal восстановления без pg_dump;
frontend: 259 PASS, typecheck/build PASS; Ruff/Black PASS.

Это техническая приёмка deployment. При передаче owner limits=null и новые
заявки не разрешены; для запуска нужны четыре общих лимита. Реальные исполнения
и свежий exact intent не проверялись. Историческая UNCERTAIN 17.10 помечена
HISTORICAL_NOT_RECHECKED. Дополнительный post-rollout bundle не получен из-за
транспортных сбоев SSH; валидный acceptance receipt, публичный health и rendered
UI доступны. Подробности и границы — в [runbook](deployment/prod-trading-start.md).

## Дополнение 16.10.2026: торговля по IN_WORK без ручного допуска — INCLUDED IN PROD RELEASE 18.10

По прямому решению пользователя отдельные ручные допуски убираются: для новой
заявки достаточно включённого режима торговли, автомата `IN_WORK`, биржевых и
автоматических риск-проверок, а также четырёх общих лимитов счёта. Canary только
GOLD, ежедневный ручной ARM/срок до полуночи и ручная сверка/recovery ack не
должны блокировать заявку. Core сохраняет достоверность брокерских данных,
общий account risk, собственные размеры лота, средства, атомарные резервы,
владельца/поколение, идемпотентность и fail-closed обработку UNKNOWN. HOLD,
READ_ONLY и отключённая стратегия продолжают блокировать новую торговлю.

Аудит до декомпозиции и решение: `develop/.planning/2026-10-16-position-driven-admission/task_plan.md`;
спецификация: `docs/superpowers/specs/2026-10-16-position-driven-admission-design.md`;
план: `docs/superpowers/plans/2026-10-16-position-driven-admission.md`.
Реализация C1 contracts, C2 Core и C3 Worker/UI завершена; независимое
кросс-ревью: C0/I0/M0. Целевой Core набор: 236 passed. Полный локальный Python
suite прошёл: 2515 passed, 196 PostgreSQL-only skipped без отдельной тестовой БД.
На завершение вехи16.10 код находился только в checkout; переключение сервисов
тогда не выполнялось. Наблюдение17.10 относилось к прежнему ручному canary/ARM
пути. Этот промежуточный статус заменён rollout18.10, описанным выше; исторические
результаты16–17.10 не подтверждают свежую runtime или live торговую приёмку.

## Дополнение 10.10.2026: процент квоты одной заявки — DEPLOYED PROD

Пользователь уточнил первоначальные «величину лота и количество лотов» до
одного процента доступной квоты в карточке открытой позиции. Подтверждено:
свободные деньги / число открытых торговых позиций; BUY, усреднение и обычный
SELL_PART ограничиваются, защитный стоп и SELL_ALL остаются без этого лимита.
Сверка root с текущим кодом показала общий `buy_order_lots`, отсутствие формы
и редактирования Core, локальное хранение активных команд Worker. Старый 0.7
без индивидуальных ресурсов заменён только этим пользовательским параметром.

Контракт: nullable `order_quota_percent` (0 < процент <= 100), NULL сохраняет
прежнее поведение. Валютные и неположительные позиции не участвуют в делителе,
минимум 1; предел номинала округляется вниз по цене биржевого лота (ask для
покупки, bid для продажи); комиссия отдельно входит в проверку доступных денег.
5000 RUB / 1 × 10% / 50 RUB = 10 лотов. Настройка обновляется у активной позиции,
сохраняется после перезапуска и влияет на будущие решения; созданные intent
не меняются. Отдельная `settings_revision` сохраняет принимаемость pending
финансовых фактов; PATCH проверяет execution и settings revisions, изменяет
только последнюю. Core 0005 и аддитивное обновление Worker SQLite имеют
проверяемый откат с отказом при потере заданной настройки.

Root актуализировал scope/DoD до делегирования; авторы `quota_core`,
`quota_worker`, `quota_frontend` (gpt-6-luna/high), общий contract writer Core.
Независимый nonauthor `rollout_ops_review` проверил Core/API/CAS, реальные
Worker composition/status refresh/стратегию, сохранность intents/outbox,
SQLite/PostgreSQL upgrade/rollback и UI race/conflict: C0/I0/M0 после исправлений.
Финансовое округление использует точный расчёт целых лотов; BUY размер задаётся
до проверки средств и стратегии. При сбое связи с Core Worker приостанавливает
новые решения до подтверждения настроек. UI сохраняет ввод при polling,
защищает сохранённые настройки от задержанных ответов и явно предлагает сброс.

Root: полный backend на Python 3.12 с отдельным PostgreSQL — 2266 PASS,
2 SKIP исторических restore rehearsal без `pg_dump`, 232 warnings; frontend —
215 PASS, typecheck/build PASS; изменённые Python-файлы Ruff/Black/diff PASS.
Свежий отдельный прогон двух PostgreSQL migration tests — PASS. Итоговая
локальная интеграция и доказательства независимо приняты `rollout_ops_review`: C0/I0/M0.
Доказательства:
`develop/reports/position-quota-20261010/`,
план `develop/.planning/2026-10-10-position-lot-parameters/task_plan.md`.
Core, Worker и интерфейс совместно выложены в PROD около14:54 МСК10.10:
`vps-20261010-quota-987a495d6c71`, рабочая схема `0005_order_quota_percent`.
Основной `https://135.136.178.252/` обслуживает PROD; listener8443 отключён,
внешний HTTPS8443 недоступен. Четыре TEST приложения stopped/restart=no,
canonical TEST READ_ONLY/strategy=false; shared PostgreSQL/collector и volumes сохранены.
Health/HTTPS/auth/CSRF/assets/позиции/поля настроек PASS; отдельный Chrome
отрисовал форму входа. Карточка после входа вручную не проверялась.
Процент пока NULL/settings_revision0. Owner generation3/revision5,
STOP_REQUIRED/recovery_only/effective_can_submit=false, lease свежая на снимке14:55.
ARM/reconcile не выполнялись; база10.10 сохранена, реальная торговля не подтверждена.
Original FAILED_BUILD/FAILED_ROLLOUT и markers сохранены; однократные продолжения
завершили только недостающие шаги без повторной миграции/сборки.
OPS: root36, затем47 для build continuation и8 для rollout continuation;
Ruff/Black/diff PASS. Independent rollout_ops_review проверил подготовку и actual receipts;
окончательная приёмка документации фиксируется в `final-review.json`.
Evidence: `develop/reports/quota-prod-rollout-20261010/` — `complete-rollout.json`,
`final-runtime-proof.json`, `external-browser-smoke.json`, `result.json`.
[Контракт и условия отката](development.md#процент-квоты-заявки).

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
| 2 | O1/S1: диагностика, ёмкость и M1 оптимизация ОЗУ — OPEN | Статус/audit доступны оператору; замеры24ч и подтверждение7дней подтверждают рост данных и потребность в ёмкости; M1 требует измеримого before/after и независимого review |
| 3 | PROD P1/P2/shared-DB/dedup: code/integration PASS | Одна Core PG/БД, scope guards/collector/Compose проверены; backend 1456/0skip, frontend 119/type/build, isolated paired fixtures 2/0skip. Deployment, рабочая парная backup/restore и ограниченные capacity/runtime проверки выполнены; initial operational acceptance PASS; PROD accounts/summary приняты; остальная PROD приёмка и длительное наблюдение открыты |
| 4 | O2/PROD P3/P4: готовность | Уведомления, лимиты/controlled takeover, восстановление и сверка реального счёта приняты |
| 5 | PROD P5: canary | Отдельный допуск владельца, численные лимиты и один разрешённый UID; расширение отдельно |

10.10 выполнен свежий аудит ресурсов, БД и торгового допуска; по запросу пользователя удалены копии. Шесть работающих сервисов занимали около 542 MiB Docker RAM; shared PostgreSQL — 8,60 GB logical / 8,71 GB physical, том SQLite PROD Worker — 0,27 MiB. Удалены два остановленных PostgreSQL restore-контейнера с их томами и 23 файла копий; освобождено 30,07 GB, свободно около 39,01 GB. Все 11 остальных контейнеров и четыре исходных тома сохранены. Это точечное измерение, приёмка 24ч/7суток O1/S1/M1 остаётся открытой. GOLD имеет IN_WORK и 18 лотов, но допуск STOP_REQUIRED/recovery_only=true/effective_can_submit=false и SDK session CLOSED; торговля в аудитe не включалась. Пользователь согласовал месячную очистку с исключениями для финансовой истории, постоянных ключей повторов и незавершённых операций. Реализован `develop/scripts/cleanup_database.py`: отчёт по умолчанию, `--apply` для удаления старых некритичных логов и снимков с сохранением последних. 17 целевых проверок изолированных PostgreSQL/SQLite и 2268 проектных тестов прошли (2 paired restore skipped: нет `pg_dump`); независимый `cleanup_ops_review` C0/I0/M0. [Правила и команды](../develop/database-retention.md). На рабочей БД SQL DELETE не выполнялся. Доказательства: `develop/reports/prod-resources-db-trading-audit-20261010/audit-report.md`, `copy-deletion.json`, `copy-post-verification.json`.

11.10 подготовлено [архитектурное предложение по БД](audits/database-architecture-20261011.md)
по свежему READ ONLY снимку PostgreSQL 16.15 и SQLite PROD Worker:
shared PG 8,611 GB, 24 таблицы/45 FK/69 индексов; три таблицы events/audit/decisions
занимают 99,43% размера. Все 1592 sampled envelopes принадлежат историческому TEST;
это не измерение текущего роста PROD. Найдены два составных CHECK, допускающих NULL,
дублирование diagnostic payload в permanent envelopes и новый постоянный audit
OWNER_LEASE_RENEWED на каждый heartbeat. Это исходный архитектурный снимок;
последующая реализация описана ниже, deployment доработок не выполнялся.
Предложенные отдельные задачи (приёмка/rollback подробно в заключении):

| Задача | Статус | Граница / DoD |
|---|---|---|
| D1: строгие bootstrap/generation CHECK | CODE_DONE / NOT_DEPLOYED | Новая миграция поверх 0005, без правки применённых revisions; isolated upgrade/downgrade, отказ без удаления несовместимых данных |
| D1b: Worker typed states / order transitions | CODE_DONE / NOT_DEPLOYED | Отдельная versioned SQLite migration и narrow policy реальных recovery/terminal edges; сохранение outbox/intents/keys |
| D2a: компактная диагностика успешного renewal | CODE_DONE / NOT_DEPLOYED | Lease обновляется/проверяется каждый heartbeat; permanent owner/ARM/STOP/expiry/denial evidence сохраняются; выбранный observability contract и before/after |
| D2b: один decision audit summary вместо шести markers | CODE_DONE / NOT_DEPLOYED | Решения/финансовые факты/critical/bootstrap/recovery сохраняются; old history не удаляется; sequence/retry и полнота summary проверяются |
| D3: измеренный portfolio run_id index | CODE_DONE / NOT_DEPLOYED | Только isolated query/insert/WAL comparison; один оправданный index, rollback; пустые/малые relations заранее не индексировать |
| D4: permanent receipt + diagnostic body | CONDITIONAL | Versioned semantic replay/retention/rollback spec; финансовая история и global dedup identity постоянны; начинать при доказанной необходимости |

11.10 архитектурная приёмка: независимые status/audit/index исследования выполнены;
финальное nonauthor cross-review `db_architecture_review` C0/I0/M0, оценка завершена. Пользователь 11.10 разрешил реализацию D1/D1b/D2a/D2b/D3
с делегированием и удаление всех TEST данных из текущей shared Core БД.
[План, scope и rollback](superpowers/plans/2026-10-11-database-improvements-and-test-purge.md):
реализация D1/D1b/D2a/D2b/D3 и task cross-review завершены; общая source-проверка
завершена: финальный TEST recovery FAILED→open→terminal край и устаревшие fixtures
исправлены и независимо перепроверены. Общие проверки: 2437 проектных + 141
служебный тест, Ruff/Black 65 Python файлов, diff check PASS; broad final source
review database_integration_review Approved C0/I0/M0. Новые source миграции 0006/0007
не применялись на VPS (live head 0005). Decision audit 6→1, successful lease audit
первое событие поколения и затем не чаще одного за 300с; heartbeat и security checks
сохранены. Изолированный benchmark run_id index: 1080→4 buffers, индекс 336 KiB,
WAL +12,25% для измеренного workload. TEST purge helper прошёл 16 isolated SQLite/PG
tests и independent test_purge_review C0/I0/M1; actual purge завершён, fresh24таблицы
TEST0/originalerrors0/allprocessbackendabsence. Собственный временный audit index
создан и снят; protected PROD18eventtail/original4volumes сохранены.
Health/ping/basicHTML/API PASS, независимое operational review purge_final_ops_review
Approved C0/I0/M0, final I1 закрыт. D4 остаётся условным; O1/S1/M1 и торговая приёмка открыты.
[Отчёт реализации](audits/database-improvements-20261011.md).

09.10 начата авторизованная реализация и раскатка P3 по
[спецификации допуска](superpowers/specs/2026-10-09-production-admission-design.md)
и [плану](superpowers/plans/2026-10-09-production-admission.md).
P3 **DONE** 09.10 в 21:55 МСК по актуальному базовому DoD: реализация и независимые Core/Worker/fact/OPS
reviews приняты без существенных замечаний. Оба полных прогона — baseline
и Python3.12.14 с фактическими 51 runtime зависимостями PROD — прошли:
2230 тестов без пропусков каждый. Native Linux образы собраны и проверены.
Пользователь отдельно подтвердил краткую остановку TEST portfolio collector.
QUIESCE 09.10 в 17:12 МСК выполнен: все пять writers остановлены; Core SIGTERM
подтверждён свежими shutdown markers, Worker/collector — exit 0, обе SQLite
без active intents/unclean runs/WAL. Узкий OPS dispatcher принят независимым
review; свежие 71 OPS/wrapper проверки и Ruff/Black прошли. Парная резервная копия и
изолированное восстановление прошли; проверка совместимости остановилась на
несовпадении hostname helper с реальным shared PostgreSQL alias. После явного
указания пользователя 09.10 продолжение restore и полные проверки целостности
исключены из DoD: проверенная миграция применяется непосредственно к рабочей БД,
после запуска проверяются health/ping, базовый интерфейс и read-only допуск
DISARMED. Rollback-скрипт обязателен каждой миграции; обычный deploy не требует
нового полного backup/restore. Рабочая миграция `0004_trading_admission` и
cutover выполнены: release `vps-20261009-p3-41dd35003304`, все девять целевых
сервисов running, health healthy/none, без OOM и рестартов. Shared PG,
volumes/caps и Analytics/frontend сохранены; обе canonical ссылки обновлены.
HTTPS UI/assets/auth/health/runtime/positions/operations/automations PASS
в TEST/PROD. PROD READ_ONLY/strategy=false/DISARMED, effective_can_submit=false;
TEST TRADE возобновлён. Автор helper `capacity_contingency`, root ops;
независимый `rollout_ops_review`: код и actual receipts/runtime C0/I0/M0;
`resume_local_checks`: документная политика принята. Root 10 новых целевых
тестов PASS, Ruff/Black/diff PASS. Доказательства:
`develop/reports/prod-admission-20261009/basic-deploy.json`,
`server-basic-runtime.json`, `basic-root-tests.log`.
Предшествовавшая ограниченная очистка 43 старых
private BuildKit cache записей сохранила все образы/контейнеры/volumes/backups;
повторный fresh capacity gate перед QUIESCE прошёл с бюджетом 24 млрд байт.
Целевой rollout — PROD READ_ONLY, strategy=false, DISARMED; TEST TRADE и
финансовая история сохраняются. Это не закрывает O2, P4 или P5. Боевой ARM
на момент закрытия P3 ожидал выбранного UID, численных лимитов и отдельной
операционной приёмки.

Дополнение 10.10: пользователь разрешил подготовить базу/сверку и включить
TRADE/допуск для открытой GOLD позиции. Broker UID подтверждён; лимиты
утверждены формулой: свободные RUB деньги / стоимость счёта / число
положительных торговых позиций / 5% базы московского дня. TEST position
stop loss не подменяет дневной лимит счёта. Core cash-position bug исправлен
и совместимый patch выложен; baseline 5052,94 RUB принята. PROD Core/Worker
TRADE/strategy=true, Analytics healthy. Передача владельца generation 2
и финальная canary сверка выполнены; активных ordinary/stop orders и
unresolved reservations 0, позиция GOLD 18 единиц. Сверка DATA_UNKNOWN /
can_arm=false; risk snapshot сообщает MARK_PRICE_STALE. ARM и установка
лимитов ожидают свежих рыночных данных. Health и базовые HTTPS функции
TEST/PROD прошли; десять сервисов running без OOM/рестартов, volumes сохранены.
Подробности и фактические receipts — [runbook](deployment/production-cutover.md).
O2/P4/P5 и длительная приёмка TRADE остаются OPEN.

Fresh readiness 10.10 20:26–20:27 МСК после релиза квоты: Core health OK,
runtime PROD/TRADE, Worker heartbeat и завершение циклов наблюдаются. Текущий
owner generation 3, lease свежа; допуск STOP_REQUIRED/recovery_only=true,
limits/reconciliation отсутствуют после передачи owner. Прямой read-only SDK
GOLD подтвердил запрет limit/market orders и MARK_PRICE_STALE (последняя
цена сделки 09.10 23:47 МСК). Active ordinary/stop orders и unresolved
reservations 0. Независимый просмотр связанных компонентов выявил отдельный
кодовый блок: risk adapter возвращает SDK ETF, а каталог и submit contract
ожидают FUND; установленная версия отклонит snapshot с DATA_UNKNOWN. Хеши
трёх deployed файлов совпали с локальным source. Нужен минимальный патч
нормализации ETF→FUND; он не расширяет allowlist и не ослабляет gates.
Патч подготовлен локально (две строки adapter); root100 связанных tests PASS,
Ruff/Black/diff-check PASS. На момент проверки 20:27 в PROD он ещё не был установлен.
Независимое cleanup_ops_review C0/I0/M0; ETF/FUND замечание закрыто локально.
После его выпуска при доступном рынке нужны свежий current-day baseline
при необходимости, CANARY_UID сверка и разрешённые caps/ARM только при can_arm=true. ARM и заявки
в этой проверке не выполнялись. Evidence:
`develop/reports/prod-trading-readiness-20261010/{api-now,risk-now,result}.json`.

Выпуск патча 10.10 21:23–21:24 МСК: Core-only релиз
`vps-20261010-etf-fund-1b9e20608a22`, установленный SDKriskGOLD теперь FUND.
Core/внешний HTTPS health, anonymous Chrome loginrender и authenticated
API позиции PASS, source243/dependencies verified. Остальные контейнеры,
ENV и volumes сохранены; Worker/PG/collector/TEST не перезапускались.
Initial attempt отказал до Corestop из-за inherited stale overlaychecksum;
originalFAILED сохранён, bounded reviewed continuation завершил candidate
marker/checksum и reused activation без rebuild/initialreplay. Code/OPS review
rollout_ops_review C0/I0/M0, root100 patch tests и21 OPS tests PASS; final
actualreview rollout_ops_review C0/I0/M0 принял технический выпуск. Торговля остаётся STOP_REQUIRED/recovery_only,
MARK_PRICE_STALE и brokerlimit/market flags false; orders/stops/unresolved0,
ARM/reconcile/orders/migrations/DBintegrity/backup не выполнялись.
Evidence: `develop/reports/etf-fund-prod-patch-rollout-20261010/` (report/result,
complete-deploy, postflight, api-after, risk-after, root-runtime-proof).
O2/P4/P5 остаются OPEN.

Свежая диагностика 13.10, 22:44–22:48 МСК: PROD Core/Worker/Analytics работают,
TRADE и стратегия включены, но допуск STOP_REQUIRED/recovery_only, ARM/лимиты/
сверка отсутствуют, база дня от10.10. База создаётся явным operatorPOST/CLI;
ежедневного автоматического создания в приложении нет. Worker пишет WAIT/NOT_OWNER из-за режима
восстановления; текущий owner generation3 совпадает с Worker, lease живая на снимке.
Дополнительный gate активации — MARK_PRICE_STALE: код требует возраст последней
сделки≤1с, SDK GOLD вернул ~73с при свежем стакане~0,7с и разрешённых api/limit/market
флагах. Старые закрытый рынок и ETF/FUND не являются сегодняшней причиной.
TEST4 подтверждённо stopped/restart=no после вывода из эксплуатации; sharedPG/collector
работают для PROD. Нужны текущая база московского дня, CANARY_UID сверка с can_arm=true,
актуальные согласованные caps/ARM; пригодность источника mark и порога1с для GOLD
требует отдельной оценки без подмены времени сделки временем запроса. В диагностике
не изменялись допуск, бизнес-данные и runtime; заявки не отправлялись. Доказательства:
`develop/reports/trading-blockers-20261013/report.md` и соседние
`result.json`, `api-now.json`, `risk-now.json`, `worker-now.json`. Независимый
prod_admission_core_review принял вывод и helper C0/I0/M0; review.md содержит
область проверки и ограничения.

13.10 23:16–23:18 МСК подтверждён отдельный Important UI дефект после
пользовательского HOLD: runtime.ts разрешает tradingAllowed только TEST+TRADE,
поэтому Resume/Close в PROD отключены при фактическом PROD+TRADE. Hold доступен
отдельно. API GOLD HOLD/resume_requested=false; тот же TEST-only guard прочитан
из действующего JS bundle. Backend resume допускает TRADE и не снимает ARM/
account admission gates. I1 OPEN: нужна ограниченная правка global capability с
проверками всех callers (Resume/Close/Create), сохраняя READ_ONLY/unknown и
Core/Worker допуска. Диагностика не выполняла Resume, patch или deploy.
Доказательства: `develop/reports/hold-resume-20261013/report.md` и receipts.

14.10 сформулирован [план задач запуска PROD](superpowers/plans/2026-10-14-prod-trading-start.md):
PT1 UI capability, PT2 risk mark/freshness, PT3 ежедневная подготовка,
PT4 причины блокировки/оповещения, PT5 сквозные проверки/кросс-ревью,
PT6 совместимый выпуск, PT7 оставшаяся read-only P4/операционная приёмка и
последующий explicit ARM/Resume/canary P5. Первый запуск допускает существующий
ручной baseline/reconcile/ARM; daily automation нужна для устойчивой работы новых дней.
Пользовательский HOLD сохраняется; automatic daily ARM требует отдельно принятой
политики. План принят независимым prod_admission_core_review C0/I0/M0,
SHA972696c... первоначального плана. Затем пользователь разрешил делегирование и
локальную реализацию до собственного merge в master; позднее14.10 отдельно разрешил deployment до merge.
PT1 локально исправлен (known PROD/TEST TRADE), 216 frontend tests/typecheck/build;
независимый prod_admission_core_review C0/I0/M0. Runtime UI I1 остаётся до выпуска.
PT2 book-only policy и реализация приняты независимым review: exact UID/настоящий unary
orderbook_ts/best ask/age0..1с, без fallback и увеличения TTL.
[Дизайн](superpowers/specs/2026-10-14-prod-trading-start-design.md) задаёт opt-in daily
preparation без ARM/Resume и Telegram (канал выбран пользователем). На этапе локальной реализации14.10
live runtime ещё не проверялся; operational источники тогда — receipts13.10.
На локальном этапе SSH/deploy/ARM/Resume/real alerts не выполнялись; тогда PT6–7 были OPEN. Обновлённый статус PT6 приведён ниже.

14.10 локально реализованы PT1–PT5: команды PROD, свежий book-only риск,
идемпотентная подготовка дня без ARM/Resume, точный Worker STOP_REQUIRED,
read-only UI допуска и optional Telegram. Новая база сохраняет STOP/recovery;
сверка при recovery остаётся явной. Automatic acceptance проверяет реальный
snapshot deadline после locks; неуспешная SQL сверка не оставляет финансовых
изменений. Пять Core Important, ошибки UI/quotes/recovery исправлены и независимо
перепроверены. Root native: 2524 project +141 develop tests, отдельная PostgreSQL;
frontend 266 tests/typecheck/build, Ruff/Black/diffcheck PASS. Исходные userdirty
сохранены; branch/stage/commit/push не выполнялись. Reviewers:
prod_admission_core_review (Core/risk/Worker/alerts/интеграция/fixtures),
prod_start_ui_review (UI), оба не авторы. Остался неблокирующий Minor дублирования
baseline cash-flow validation; текущие правила совпадают. Evidence:
`develop/reports/prod-trading-start-implementation-20261014/`, final45 SHA manifest.
После локальной приёмки пользователь изменил порядок: deployment до merge.
PT6 **DONE14.10**, PRODtagvps-20261014-prod-start-b2f54029c729: Core/Worker/frontend
обновлены, необходимые проверенные PG0006/0007 и WorkerSQLite1 применены.
Health/progress/HTTPUI+read API PASS; независимый prod_release_architect C0/I0/M0.
Ownergeneration4/revision6, HOLDrevision3/noResume и STOP_REQUIRED/recoverytrue
сохранены; sharedPG/collector/Analytics прежние, TESTretired, daily/Telegramdisabled.
Source353manifest/imageIDs/envSHA/volumes подтверждены; отчёт
`develop/reports/prod-trading-start-deploy-20261014/report.md`.
Merge делает пользователь. PT7 OPEN: Telegram recipient/доставка, opt-in daily,
remaining read-only P4, stop/UNKNOWN и explicit ARM/Resume/P5. Визуальный browser
и live trading не проверялись; обычный deploy не включает fullDBintegrity/restore.

Уточнение14.10,19:54MSK: GOLD иSIBN уже IN_WORK, accountSTOP_REQUIRED/recovery-only,
baseline10.10/reconARMlimitsnull; Workerздоров. Журнал подтверждает10.10неуспешную
CANARYсверкуDATA_UNKNOWN и14.10controlledhandoff с повторнымrecovery. ТекущийARM
разрешаетодинUID: immediateexistingAPI запуск возможен для выбранногоинструмента.
ДляодновременнойторговлиGOLD/SIBN proposedрасширениеmultiUIDadmission/сверки/лимитов;
дляunattendedсменыдня/Workerowner proposedpolicy+реализацияautoARM. Dailypreparation
сам ARM не выдаёт. Writtenплан ифакты: `develop/reports/prod-trading-blockers-after-deploy-20261014/restore-trading-plan.md`,
independentprod_release_architect C0/I0. Это уточнениеscope, новыефункции не реализованы,
baseline/reconcile/ARM/конфиг в диагностике не менялись.

15.10 завершена локальная доработка **ENV, документация и автоподготовка базы**:
[план ENV1–ENV4](superpowers/plans/2026-10-15-runtime-environment-configuration.md),
[каталог параметров](configuration/environment.md). Core/Worker/Analytics
получают эксплуатационные параметры через Settings → потребители → Compose;
обновлены три шаблона, исправлено игнорирование настроек пула БД HTTP Core.
Проверки root: 2617 backend + 141 develop на отдельной локальной PostgreSQL,
275 frontend PASS, typecheck/build и Ruff/Black PASS. Независимое итоговое
ревью env_core_reviewer: C0/I0/M0; I1 Worker URL и M1 документации закрыты.
Предыдущий код был закоммичен пользователем (HEAD `2f25bfd`); агенты Git не меняли.

15.10 по новому явному поручению пользователя выполнен ENV5 deployment до merge:
`vps-20261015-env-4a71589e8766`, Core/Worker/Analytics. Frontend, shared PG,
Collector, volumes и четыре остановленных TEST-контейнера сохранены. Схема0007 /
WorkerSQLite1 без миграций. Приватный PROD ENV изменён только daily=false→true,
с сохранением режима TRADE, credentials и пустого canary. Старый Worker exit0,
новый owner generation5/revision8 передан штатно в recovery-only. Техническая
приёмка health/UI/source+image hashes/progress PASS; независимый
`env_core_reviewer` C0/I0/M0. Отчёт: `develop/reports/runtime-env-deploy-20261015/report.md`.

Автоматика создала базу за 15 октября в 19:06:54 МСК, BASELINE_REQUIRED устранён.
На 19:08:49 МСК торговля остаётся закрыта: DISARMED, STOP_REQUIRED,
RECONCILIATION_REQUIRED, нет reconciliation/ARM/limits. Обе позиции IN_WORK,
повторный Resume не нужен; свежих операций в возвращённой broker-operations
выборке нет. ARM/Resume/лимиты/принудительные заявки не выполнялись. PT7 остаётся
OPEN: явная recovery/сверка, утверждённые UID/лимиты, отдельный ARM и торговая
приёмка. Multi-UID и auto ARM пользователь явно отложил.

15.10 последующим явным поручением пользователя выполнена штатная CANARY-сверка
GOLD в 19:20:25 МСК: CANARY_READY/can_arm=true, active orders/stops/unresolved=0.
Счёт перешёл STOP_REQUIRED/recovery-only/revision8 → **DISARMED,
recovery_only=false, revision9**. Повторный GET в 19:21:50 МСК после окончания
30-секундного срока сверки подтвердил false; owner generation5 и позиции/настройки
сохранены. Worker OK, outbox CLEAR. Независимый env_core_reviewer C0/I0/M0.
ARM/лимиты/торговые поручения не задавались. Задача снятия recovery-only DONE;
торговая приёмка остаётся открытой. Proof:
`develop/reports/prod-reconciliation-20261015/report.md`, `review-final.md`.

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
Наблюдаемость, retention и P4/P5 остаются открытыми; P3 READ_ONLY rollout
завершён 09.10 по актуальной базовой приёмке выше. Initial PROD развёрнут
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

06.10.2026 — **LOCAL DONE: исправления слоистой архитектуры/SOLID**.
По принятому [аудиту](audits/layered-architecture-solid-20261006.md) и явной
команде пользователя «тогда исправим эти замечания» закрыты I1–I3 и M1–M6.
Scope, владельцы файлов, зависимости и критерии приёмки — в
[плане исправлений](superpowers/plans/2026-10-06-layered-solid-fixes.md).
Работа идёт в текущем checkout: новые ветки, включая worktree с новой веткой,
разрешены только отдельным явным распоряжением пользователя. Коммиты делает
пользователь; агенты не выполняют stage, commit или push. Локальные RED/GREEN
не означают окончательную приёмку: требуется независимое ревью интеграции.
Новый пакет не возобновляет завершённые A1/C1–C4/W4 и не включает W3/C5,
frontend, миграции, стратегию, deploy или проверку live runtime.

Локальные RED/GREEN закрыли все девять finding. Финальный independent review
полной интеграции, трёх closeout-документов и role/model matrix: **PASS,
Critical 0 / Important 0 / Minor 0**. Root принял эту кодовую веху. Проверка
до запуска отдельной тестовой БД: 1825 passed, 166
skipped, 17 warnings, exit 0; все skips обусловлены отсутствием
`POSTGRES_TEST_DATABASE_URL`, поэтому PostgreSQL не проверен. Ruff, Black check
и diff check прошли; reviewer подтвердил 448 source/test hashes без изменений и
чистый HEAD diff check. Матрица и receipts — в
[отчёте исправлений](audits/layered-solid-fixes-20261006.md). PostgreSQL,
runtime и deployment этой проверкой не подтверждены.

06.10.2026 — дополнительная проверка на отдельной Compose PostgreSQL16.15:
**1991 passed, 0 failed, 0 skipped, 17 warnings**, exit0. PostgreSQL-набор:
187 PASS, включая все166 ранее пропущенных сценариев и оба paired backup/restore;
pg_dump/pg_restore16.15 совпадают с сервером. Project
`moex-sentinel-test-20261006`, loopback127.0.0.1:55433 и literal volume
`moex-sentinel-test-20261006-data` изолированы от рабочего Compose; запускается
только database. Frozen448 source/test hashes не изменились. Evidence:
`develop/reports/postgresql-compose-20261006/verification/`. Независимый `/root/postgres_compose_review`
принял изоляцию, доказательства, документы и read-only постановку следующего
объёма: **PASS, Critical0 / Important0 / Minor0**. Тесты исполнял root; reviewer
независимо проверил XML, хеши и receipts. Локальная PostgreSQL-проверка
не означает проверку production runtime или deployment.

Предложение следующего объёма после свежей read-only сверки06.10
`/root/next_development_plan`: F2.1/F3.1 уже реализованы, повторно их не включать.
Остаточный UI-кандидат — broker editor: поздний save/error не закрывает новую
форму и не меняет её draft. Далее W3 и C5 можно исполнять независимо:
W3 — tracking SQLite offload с сохранением порядка intent/outbox;
C5 — cursor cycles/page/time budgets и ограниченная остановка collector,
без принятия неполной истории за полный snapshot. Численные бюджеты C5
закрепить перед реализацией. W5/F4 остаются условными после CPU/I/O профиля.
Подробная актуальность/DoD: `develop/reports/postgresql-compose-20261006/findings.md`.
Это предложение порядка; новая реализация этим вопросом пользователя не запущена.

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
Полные M1/O1/O2/PROD P4–P5 остаются открытыми; P3 READ_ONLY rollout завершён
09.10 по актуальной приёмке выше. W3/C5 и условные W5/F4 не входят
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

16.10 ENV7 / PT7: по явному поручению пользователя установлены четыре caps по
ранее утверждённой формуле на свежих данных PROD, штатные CANARY reconcile→ARM
GOLD в12:05:36МСК. Значения5001.1RUB/order,5052.3RUB/exposure,1outstanding,
252.6040RUB/daily loss (5%baseline5052.08); expiry17Oct00MSK.
Fresh12:06:34MSK послеTTL: ARMED/arm12/effective=true/recovery=false/denial[],
ownergen5 иcaps сохранены; daily baseline16Oct создана автоматически00:00МСК.
WorkerOK/61018COMPLETED, outbox18freshpending/0failed/0retry. Новая исполненная
сделка не подтверждена, полноценная торговая приёмка не объявляется завершённой.
Pre-execution+final independentenv_core_reviewer C0/I0/M0;21+14mocktests и
16artifactchecksPASS. Proof develop/reports/prod-limits-arm-20261016/report.md.
AutoARM/multiUID/Telegram остаются отдельно; новый день/сменаowner потребуют
штатного нового допуска. Ручные SDKзаявки/DBedits/restarts/Resume не выполнялись.

16.10 ENV8 / PT7, текущая read-only проверка12:24–12:33МСК: accountARM12
сохраняется/effective=true/recovery=false/no denies/caps/base/owner5same,
Worker62603COMPLETED/OK; outboxfinal2freshpending/0failed/0retry. Однако успешная
live-canary OPEN: GOLDформируетSELL_ALL/STOP_LOSS18lots; к12:32 зарегистрирован
401localCANCELLED/zero executed/no recordedbrokerIDs плюс1SUBMITTING.
Двасохранённыхproofs PERMIT_EXPIRED_BEFORE_POST доказываютexpiryдоSDK; уостальных
399конкретныйCoredenialreasonнесохраняется. Не приписыватьимоднуTTLпричину
бездоказательства. SIBNWAIT/UID_NOT_ALLOWED соответствуетsingleGOLDscope.
Следующийнеобходимыйscope: safe SubmitDenied enum+stage latency observability,
затем устранениеподтверждённойзадержкибезослабленияTTL иштатнаяliveприёмка.
ПовторныйARM/reconcile/Resumeнеисправляетэтотцикл; вENV8writesнебыло.
Independentenv_core_reviewer C0/I0/M0; sourceinterpretationenv_core_author.
Proof develop/reports/prod-trading-check-20261016/report.md.
