# Задачи для запуска торговли PROD

> **For agentic workers:** Использовать superpowers:subagent-driven-development. 14.10 пользователь разрешил локальную реализацию и делегирование PT1–PT5; позднее разрешил deployment проверенного checkout до собственного merge. Merge выполняет пользователь; торговая приёмка остаётся отдельным этапом.

**Goal:** Устранить подтверждённые блоки PROD, провести контролируемый первый запуск и подготовить дальнейшую ежедневную работу.

**Architecture:** Сохранить Core владельцем допуска и риска, Worker — единственным исполнителем SDK поручений. Использовать существующие baseline/reconcile/ARM и финансовые ограничения; frontend отражает разрешённые команды и причины блокировки. Новую политику risk mark и ежедневного запуска определить до реализации соответствующих задач.

**Tech Stack:** Python/FastAPI/SQLAlchemy/PostgreSQL, Worker SQLite/outbox, T-Bank SDK, Vue/TypeScript/Vitest. Новые зависимости для этих задач не требуются.

**Spec:** [Дизайн реализации](../specs/2026-10-14-prod-trading-start-design.md), [исходные gates P3/P4/P5](production-api.md), [контракты допуска P3](../specs/2026-10-09-production-admission-design.md), [актуальная очередь](../../phase-1-implementation-plan.md), диагностика `develop/reports/trading-blockers-20261013/` и `develop/reports/hold-resume-20261013/`.

## Актуальность и границы

**Локальный результат 14.10:** PT1–PT5 реализованы и проверены для пользовательского
merge. Python: 2524 project +141 develop тестов на отдельной PostgreSQL; frontend266,
typecheck/build и scoped Ruff/Black/diffcheck PASS. Все существенные замечания
исправлены; независимые reviewers prod_admission_core_review и prod_start_ui_review.
Неблокирующий остаток — одинаковая cash-flow validation двух baseline путей.
Новая схема в PT1–PT5 не добавлялась. PT6 выполнен14.10 по дополнительной
авторизации deployment до merge: tagvps-20261014-prod-start-b2f54029c729,
PG0007/WorkerSQLite1, health/HTTPUI/progress PASS, независимый opsreview C0/I0/M0.
Проверенные pending0006/7 были конкретной dependency текущего Core.
HOLD/STOP_REQUIRED сохранены, daily/Telegramdisabled; PT7 и реальные уведомления OPEN. Подробный локальный отчёт `develop/reports/prod-trading-start-implementation-20261014/report.md`.

При первоначальной постановке14.10, до разрешения и выполнения PT6, сверены исходники, очередь и CURRENT. Тогда последние runtime доказательства относились к13.10: PROD TRADE/strategy=true, GOLD HOLD после команды пользователя; account STOP_REQUIRED/recovery_only, база10.10, нет limits/reconciliation/ARM. TEST4 выведены из эксплуатации. На SDK snapshot MARK_PRICE_STALE: last trade~73с при кодовом пороге1с, хотя стакан свежий и торговые флаги разрешены. В прежнем действующем JS UI тогда оставался TEST-only tradingAllowed. Эти утверждения описывают состояние до выпуска; актуальный runtime14.10 подтверждён PT6 receipts выше.

Первые PT1/PT2 и проверенный ручной сценарий подготовки позволяют перейти к контролируемому запуску. PT3 нужен для последующей работы по новым торговым дням без ежедневного ручного создания базы. Операционные baseline/reconcile/ARM уже реализованы: повторно строить admission subsystem не нужно.

## Global Constraints

- Не создавать ветки/worktreebranches и не stage/commit/push; коммиты выполняет пользователь.
- Сохранить пользовательский HOLD: deployment/смена дня/перезапуск не являются Resume.
- Сохранить UID allowlist, явные лимиты, owner/generation/lease, revoke, RTT deduction, final clock после locks и короткий permit. Consumed key остаётся постоянным; истечение lease не передаёт владение.
- Не менять timestamp старой сделки на время получения ответа; unknown/stale риск закрывает допуск.
- Baseline — первая принятая RUB equity московского дня; внутри дня её нельзя сбросить для обхода дневного убытка. Поздний запуск не объявлять учётом полного P&L от полуночи.
- Прежние формулы лимитов разрешены, но старые суммы не переиспользовать: max order=free RUB−blocked; exposure=equity; outstanding=число положительных noncurrency позиций; дневной убыток=5% свежей базы. Учитывать действующие reservations и комиссии в существующем submit gate.
- Реальные SDK мутации выполняет только Worker. Никаких прямых тестовых POST/cancel из Core или диагностических scripts.
- DB тесты только на отдельном экземпляре. Миграция при необходимости имеет проверенный downgrade/rollback с отказом при риске потери новых данных.
- Обычная приёмка deployment — health, ping, базовый UI. Полный backup/restore/финансовое сравнение не добавлять в каждый выпуск; аварийное восстановление следует отдельному регламенту.

## Review Focus

- Новый день, повторный старт, два процесса: ровно одна база дня, без сброса уже накопленного дневного убытка.
- HOLD/revoke/новый runtime UUID: автоматизация не возобновляет остановленную позицию и не захватывает account ownership.
- Неликвидный инструмент/свежий стакан/старая сделка и реально устаревший рынок: источник valuation и настоящий возраст данных проверяются отдельно от срока permit.
- Core outage, потеря ACK, partial fill и pending outbox: нет повторной SDK отправки; финансовая история и резервы сохраняются до доказанной сверки.
- PROD TRADE, READ_ONLY, unknown/loading runtime: UI и сервер согласованы; доступная кнопка не заменяет account ARM/лимиты.

## PT1 — исправить команды интерфейса PROD

**Files:** `frontend/src/runtime.ts`, `frontend/src/runtime.spec.ts`; callers/tests `PositionsListView.vue/.spec.ts`, `InstrumentItemView.vue/.spec.ts`.
**Contract:** `tradingAllowed` допускает команды для известных TEST/PROD в TRADE; READ_ONLY, неизвестный режим и загрузка закрыты. Resume требует HOLD и отсутствия уже принятого запроса; Hold сохраняет отдельную возможность остановки. Global change затрагивает также Create/Trade и Close.
- [x] Regression PROD TRADE: RED 3 ожидаемых отказа; целевые 45 тестов GREEN.
- [x] Согласовать capability с сервером; проверены все callers и повторные запросы.
- [x] Frontend 216/216, typecheck/build; независимый prod_admission_core_review C0/I0/M0. Локальный ACK, runtime не проверялся.
**DoD:** При выбранной HOLD позиции Resume доступен в PROD TRADE, причины disabled понятны; READ_ONLY/unknown закрыты. Core/Worker admission не ослаблен.

## PT2 — исправить источник и проверку свежести risk mark

**Files:** `src/moex_sentinel/adapters/tinvest/admission_risk.py`, `tests/adapters/test_tinvest_admission_risk.py`. 14.10 SDK/consumers сверены: общий контракт менять не требуется.
**Contract:** Один согласованный источник RUB valuation и правило возраста для canary reconcile и submit/exposure. Короткий permit1с/RTT/lease сохраняются; численный возраст risk valuation выбирается по источнику/его семантике, не увеличивается вслепую.
- [x] Book-only: настоящий unary `orderbook_ts`, exact UID, валидные обе стороны, best ask как консервативный LONG mark, возраст 0..1с. Без LastPrice fallback. Независимый policy ACK C0/I0.
- [x] Regression last trade~73с/fresh book~0.7с, invalid/crossed/missing/stale/future SDK quotes, canonical units/nano и late-cursor expiry; adapter52/risk gates13 PASS.
- [x] Минимальный адаптерный контракт и независимый financial ACK; исходный deadline не продлевается под locks.
**DoD:** Одно отсутствие новой сделки не считается устаревшим рынком при пригодном свежем источнике цены; действительно stale/unknown данные закрывают допуск. Reconcile и submit применяют одну политику; price/time не подделываются.

## PT3 — подготовка нового московского торгового дня

**Files:** reuse `services/trading_admission.py`, `storage/repositories/trading_admission.py`, `operator_admission.py`, существующая Core composition. Точная точка единственного daily orchestrator определяется в проектировании; Worker не получает операторские credentials и право менять account baseline. Tests: `tests/services/test_trading_admission.py`, native `tests/integration/postgresql/test_trading_admission_concurrency.py` и новые тесты выбранного orchestrator.
**Contract:** Автоматически подготовить новый день по согласованной политике: authoritative account snapshot → первая baseline дня → свежая сверка и расчёт лимитов. Принятая база сохраняется при повторном старте. ARM/Resume не возникают как побочный эффект сбора базы.
- [x] Scoped opt-in Core orchestrator: first attempt, day checks≤60с/backoff60–300с/reset, bounded attempt, SDK/event loop и DB/executor; точные files/interfaces согласованы.
- [x] Daily ARM остаётся явным; автоматического продления/Resume нет.
- [x] Rollover/двойной вызов/restart/cashflow/finalclock/STOP preservation: unit/API и 6 native PostgreSQL cases PASS, первая база immutable.
- [x] Read-only preparation/current identity/day/lease/proof/preview caps; independent Core I1–I5 closure.
**DoD:** Новая база не остаётся старой только из-за отсутствия ручной команды; текущая база/история защищены, при ошибке торговля закрыта, HOLD не снимается. Для первого запуска можно использовать уже существующий ручной сценарий.

## PT4 — причины блокировки, мониторинг и уведомления

**Files:** `src/trading_automaton/services/trading_admission.py`, связанный adapter/materializer tests; existing API `views/trading_admission.py` и frontend runtime/позиции — минимальный read-only вывод имеющихся полей. Уведомления — относящийся к запуску scope [O2](../../deployment/observability-plan.md), точные channel/recipient/files выбираются до реализации, без автоматического добавления полного стека dashboards.
**Contract:** NOT_OWNER относится к чужому/невалидному owner; recovery_only отображается как STOP_REQUIRED. Показать отдельно HOLD позиции и допуск счёта: дата базы, лимиты, сверка, ARM и market/risk причина. Никаких финансовых действий из диагностики.
- [x] Current owner+recovery_only: WAIT/STOP_REQUIRED; mismatched owner NOT_OWNER, Worker regression PASS.
- [x] Конкретный read-only UI блок, valid/unknown/malformed/expired/out-of-order данные; независимый UI ACK, safe Telegram lifecycle/dedup/retry/recovery unit tests PASS.
- [ ] До canary определить согласованный канал и получателя уведомлений; проверить critical stop, потерю heartbeat/progress, FAILED/pending outbox, UNKNOWN и восстановление. Записать команду остановки и процедуру UNKNOWN; тестовое сообщение только после разрешения владельца на выбранный канал. Независимое ревью поведения/UI/сценариев оповещения.
**DoD:** Оператор понимает причину WAIT/disabled и отличает HOLD от закрытого допуска; согласованный получатель получает сообщения о проверенных критических отказах/восстановлении. Остановка и UNKNOWN процедура воспроизведены до canary; health процесса не выдаётся за готовность торговли.

## PT5 — сквозная проверка изменений и кросс-ревью

**Files:** existing frontend/runtime/list tests, SDK/risk tests, `tests/integration/postgresql/test_trading_admission_flow.py`, `test_trading_admission_ingress.py` и isolated migration tests если нужна DDL.
- [x] HOLD→Resume capability отдельно от account gates; rollover/owner/lease/revoke/source freshness покрыты локально.
- [x] Existing ACK/partial/restart/outbox/permanent-key regressions в полном native project наборе; факты/резервы/закрытый допуск сохранены.
- [x] Native project2524 +develop141 и frontend266/typecheck/build, formatter/linter PASS; все C/I исправлены и независимо перепроверены.
**DoD:** Нет открытых C/I в выпускаемом scope; тесты на отдельной БД и проверенных runtime dependencies. Не повторять несвязанные suites без изменений/основания.

## PT6 — совместимый выпуск PROD

**Owner:** root ops; reviewed release/helper и существующие canonical Compose wrappers/runbook.
- [x] Сверить runtime/источники/зависимости/volumes и собрать проверенный артефакт. TEST остаётся retired, shared PG/collector сохраняются.
- [x] Выпустить минимально нужные сервисы с сохранением пользовательского HOLD и финансовой истории. Если нужна миграция — заранее проверенный apply/rollback на отдельной БД; working downgrade/restore не выполнять по умолчанию. При обновлении Worker его новый runtime UUID требует штатной controlled transfer: закрытый допуск (при активном ARM — revoke), подтверждённая остановка прежнего owner, регистрация кандидата и явная передача в recovery-only; сверка/ARM затем в PT7. Один restart не передаёт ownership.
- [x] Health/ping и базовый интерфейс через HTML/assets + read APIs: correct runtime и HOLD; UI capability покрыт266frontend tests; no real control POST в deployment check. Независимое ревью фактического выпуска.
**DoD:** Выполнено14.10 (report develop/reports/prod-trading-start-deploy-20261014/report.md, reviewers prod_release_architect C0/I0/M0): изменения развернуты, HTTP интерфейс открывается, нужные сервисы работают; технический deployment не объявляется успешной торговой приёмкой. Source0006/0007/retention/CI-CD не добавлять в критический путь без конкретной зависимости.

## ENV — конфигурация и автоподготовка базы (15.10, LOCAL DONE)

Пользователь подтвердил отдельную доработку: **ENV всех сервисов, документация
каждого параметра и автоподготовка базы**. План реализации:
[ENV configuration](2026-10-15-runtime-environment-configuration.md),
[дизайн](../specs/2026-10-15-runtime-environment-configuration-design.md).
ENV1 Core, ENV2 Worker/Analytics, ENV3 Compose/каталог/шаблоны, ENV4 root integration
и независимое кросс-ревью завершены. Значения доходят до потребителей, включая
исправленные настройки пула Core HTTP. Проверки: 2617 backend + 141 develop +
275 frontend PASS, typecheck/build и Ruff/Black PASS. Итоговое независимое
ревью env_core_reviewer: C0/I0/M0. [Каталог ENV](../../configuration/environment.md).

Daily scheduler уже реализован. Fresh15.10 09:39MSK: actual PROD flag=false,
поэтому ежедневная ручная база не является целевым решением — требуется явная
активация существующей автоподготовки. Эта доработка документирует и задаёт
PROD example profile daily=true/emptycanary, сохраняя safe app/Compose defaults.
Применение приватного ENV на VPS и приёмка фактической базы дня — отдельный
операционный шаг до ARM; наличие нового кода не подтверждает активацию.

15.10 последующим поручением выполнен ENV5: релиз
`vps-20261015-env-4a71589e8766`, Core/Worker/Analytics, техническая приёмка и
независимое ревью C0/I0/M0. Daily=true создал базу за 15 октября в 19:06:54 МСК.
На 19:08:49 МСК торговля ещё закрыта: STOP_REQUIRED/recovery-only, нет
reconciliation/ARM/limits; обе позиции IN_WORK, новых операций в полученной
выборке нет. PT7 не закрыт. Доказательства:
`develop/reports/runtime-env-deploy-20261015/report.md`.

MultiUID для GOLD/SIBN и автоматический ARM после смены дня/Workerowner
**отложены пользователем**. Они не входят в ENV1–ENV4, остаются следующими
задачами; daily baseline/reconciliation не выдаёт торговый ARM.

## PT7 — подготовить допуск и начать контролируемую торговлю

**Existing interfaces:** operator `baseline`, `reconcile`, `arm`, `view`; UI Resume; Worker штатный SDK execution/outbox. Это операционный этап, не повторная реализация Core admission.
- [ ] Свежая проверка текущего MSK дня/рынка, выбранного GOLD UID, счёта, owner/gen/lease, active ordinary/stop orders, unresolved reservations, токена/доступа. Instrument trade flags/read-only RPC не считаются доказательством права на post.
- [ ] Сначала зачесть актуальные уже принятые доказательства и закрыть только оставшиеся P4 gates в режиме без broker writes: принять отчёт по выбранному счёту/сверке с кабинетом, полный торговый день и закрытый рынок, UI/market/facts/outbox/alerts и отсутствие app-created broker mutations. Результаты согласовать с владельцем и независимым reviewer; связанные с торговым запуском O1/O2/S1 условия сверить с текущей очередью, не втягивая несвязанные вехи или повторяя уже принятые проверки. Зафиксировать operator alert recipient, проверенную команду остановки и UNKNOWN procedure; состояние резервного копирования относится к отдельному действующему регламенту, не к обязательному full-restore каждому deployment. P3/P4 acceptance отсутствует — canary закрыт.
- [ ] После P3/P4 acceptance и при действующей авторизации владельца на account/UID/лимиты принять current-day baseline и успешную CANARY_UID сверку с can_arm=true; установить лимиты по ранее утверждённым формулам и свежим данным; явный ARM в срок действующей сверки. Отказ/unknown не обходить.
- [ ] По freshruntime определить состояние позиции: GOLD/SIBN14.10 уже IN_WORK, повторный Resume не нужен; если пользователь снова выставил HOLD, возобновляет его явно. Затем контролируемая первая штатная операция в согласованных пределах. Проверить прохождение strategy→permit→SDK→terminal/частичное исполнение→outbox→Core, комиссии и отсутствие дубликата. При неопределённом результате сохранить reservation и выполнить read-only recovery.
- [ ] После initial canary P5 наблюдать дальнейший live-день: контроль остановки, восстановления, дневного убытка/outbox; PT3 отдельно принимает наступление следующего дня. Последующее расширение отдельным решением.
**DoD:** Для закрытия initial canary нужен реальный подтверждённый цикл исполнения; права на post подтверждаются штатной операцией, а не подменным probe. При отсутствии сигнала наблюдение и торговая приёмка остаются открытыми, принудительную заявку ради теста не создавать. Предторговый full-day без broker writes — P4, первая ограниченная реальная торговля — P5, следующий день — приёмка PT3. Успех deployment этого не заменяет.

15.10 в 19:20 МСК по явному поручению пользователя выполнена успешная GOLD
CANARY-сверка: **recovery-only снят**, STOP_REQUIRED→DISARMED/revision9,
active orders/stops/unresolved=0, same owner generation5. Повторная проверка
19:21:50 после TTL подтвердила recovery_only=false, Worker OK/outbox CLEAR;
ARM и лимиты отсутствуют. Сверка для снятия recovery выполнена, остальные части
PT7 не закрыты. Independent review C0/I0/M0; evidence
`develop/reports/prod-reconciliation-20261015/report.md`.

## Порядок

PT1/PT2 и PT3 реализуются в согласованных независимых scope. PT4: выбран Telegram, реальные credentials/доставка проверяются после merge/deploy. Выпускаемый пакет проходит PT5→PT6. Первый запуск PT7 возможен с ручной подготовкой existing API только после оставшейся read-only P4/мониторинговой приёмки, затем explicit ARM/Resume и canary P5; PT3 необходим до обещания автономной ежедневной работы. Root фиксирует точный scope/владельца и reviewer; новые branches и Git mutations запрещены.
