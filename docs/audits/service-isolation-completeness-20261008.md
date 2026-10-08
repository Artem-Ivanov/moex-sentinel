# Изоляция сервисов и завершённость доработок — 08.10.2026

Текущий кодовый пакет I1–I3/M1–M6 завершён локально: все девять пунктов
исторической вехи подтверждены текущим кодом и тестами. Свежий полный backend
запуск прошёл: 1991 passed, 0 failed, 0 skipped; PostgreSQL-набор содержит 187
проверок, оба paired-restore сценария прошли. При этом техническая изоляция
сервисов неполная: внутренние Core routes не требуют service identity, Core API
контуров TEST и PROD доступны друг другу через общую Docker network, а текущий
broker iteration не ограничивает активные уникальные инструменты размером
страницы Analytics. TEST и PROD сохраняют отдельные Core API и общую
PostgreSQL/database. Эти findings требуют отдельного решения и не отменяют
локальную приёмку старых девяти пунктов.

Проверка охватывает исходники, текущую Compose-конфигурацию и локальные тесты на
08.10.2026. VPS/deployment и работающие production-сервисы не инспектировались.
Результаты не являются доказательством runtime-эксплуатации или полной
изоляционной гарантии.

## Границы и критерий

Ожидаемая нагрузка по постановке: один оператор, несколько брокеров, без
авторитетного лимита на число инструментов в командах. Core владеет PostgreSQL и
доменными API; Worker (`trading-automaton`) — durable SQLite/outbox и
исполнением; Analytics — расчётом рыночных snapshot без credentials и
постоянного хранилища. TEST/PROD используют одну PostgreSQL/database по
согласованному scope. Общая база сама по себе не считается дефектом, но границы
авторизации и сетевой достижимости должны быть явными.

| Уровень | Наблюдаемое состояние | Оценка |
| --- | --- | --- |
| Процесс | Core, portfolio snapshot worker, Analytics и trading automaton запускаются отдельными Compose-сервисами; Worker имеет собственный SQLite volume. | Разделение процессов и владения ресурсами есть; это не аутентификация межсервисных запросов и не граница сети. |
| Контейнер / сеть | Compose публикует frontend на loopback; remote overlay подключает два отдельных Core API (TEST и PROD) к внешней `moex-sentinel-data` network. Python runtime containers работают как `sentinel`; frontend использует `nginx-unprivileged`, database — официальный image. Analytics и frontend имеют `read_only`, Analytics без volume. | У Core API нет host-публикации в этих файлах, но peers общей сети могут достичь API другого контура. Явные resource quotas не заданы. Достижимость выведена из конфигурации, не проверена на runtime. |
| Хранилище | TEST и PROD Core используют отдельные API поверх одной PostgreSQL/database; snapshot worker также обращается к PostgreSQL; Worker использует SQLite; Analytics не имеет постоянного volume. | Разделение владения хранилищем есть, но общая БД остаётся общей точкой отказа/ёмкости. Database roles, grants и RLS не задаются репозиторием; фактические runtime grants неизвестны. |
| Контракты кода | DTO и adapter boundaries разделяют market snapshot и торговые данные; Analytics получает market-only запрос, но может вызвать Core internal endpoints. | Контракты данных ограничивают штатный payload, но сами по себе не обеспечивают ACL и identity. |
| TEST/PROD и доступ | Core middleware защищает `/api/`; internal routes вне этого префикса пропускаются. Есть environment guard для broker connection и PROD `READ_ONLY` guard. | Для внутренних API нет service-auth в проверенном пути. TEST/PROD Core API доступны друг другу из общей сети. |
| Транзакции | Проверенные тесты запускают PostgreSQL 16.15 с default `READ COMMITTED`; атомарность торгового цикла и outbox покрыта локальными тестами. | Это уровень тестового экземпляра и кодовых транзакций. Не подтверждает production isolation level, grants или конкурентность реального runtime. |
| Задержки и отказ | Синхронные операции Core ограничены executor ёмкостью 4 (`src/moex_sentinel/api/sync_execution.py:12–17`); Worker сохраняет durable SQLite/outbox, а SQLite offload остаётся открытой задачей W3. Полный набор тестов прошёл, но не является нагрузочным измерением. | Общая PostgreSQL/database связывает доступность и ёмкость TEST/PROD. Ошибка Analytics может сорвать preparation broker iteration при слишком большом списке контрактов. Runtime SLA/latency не измерялись; явные container resource quotas не заданы. |

## Findings

### Important S1 — внутренний маршрут Core отдаёт broker credential без service-auth

`src/moex_sentinel/api/app.py:183–187` применяет browser auth только к путям
`/api/`; прочие пути пропускаются middleware. В
`src/moex_sentinel/views/internal_automaton.py:29–33` маршрут
`GET /internal/automaton/brokers/{broker_id}/connection` вызывает просмотр connection, а
`src/moex_sentinel/services/automaton_brokers.py:45–53` возвращает объект с
broker token. В показанном пути не видно проверки service identity или
маршрутной ACL.

Сценарий: скомпрометированный Analytics container знает `source_id` (UUID
брокера) из штатного контракта и при сетевой достижимости Core вызывает этот
маршрут без browser cookie. Ответ содержит broker credential. Это сценарий
компрометации контейнера/сервиса; root подтвердил путь получения credentials,
но не доказывал сетевую достижимость из Analytics runtime. Утверждений о
доступности маршрута через browser/frontend proxy здесь нет. Отдельные
environment/READ_ONLY проверки снижают допустимые операции, но не скрывают
выданный credential.

Отсутствие service-auth относится также к typed facts и heartbeat на internal
API: scope/revision в контракте не удостоверяют caller. Поэтому скомпрометированный
peer с сетевым доступом потенциально может подделать сообщения от имени другого
worker. Root reproduction проверил credential route в актуальном TestClient с
synthetic credentials (public route `401`, internal route `200`, token match),
но fake-facts/heartbeat proof не выполнялся; этот риск основан на route
middleware/contract анализе, не на проверенном spoof-сценарии.

Минимальное исправление: ввести отдельные credentials и роли service identity
с узкой ACL для внутренних маршрутов; не использовать один общий секрет для
всех сервисов. Повторно проверить, что Analytics может получить только
необходимый market snapshot, а credential route разрешён только назначенному
Worker.

### Important S2 — общая Docker network соединяет отдельные Core API TEST/PROD

`deploy/remote/compose.remote.yml:23–26` присоединяет backend к сетям `default`
и `data`; строки 56–59 ссылаются на внешнюю сеть `moex-sentinel-data`.
`deploy/remote/compose.production.yml` не переопределяет это присоединение.
Миграции и snapshot worker также подключены к `data` в remote overlay
(`compose.remote.yml:19–20,31–36`). Core listener принимает внутренние запросы
на `8000`, а compose-проверка здоровья обращается к `127.0.0.1:8000`; Core API
не публикуется на host-порт в remote overlay, но доступен участникам общей
Docker network.

Сценарий: TEST Core, collector или migrations container, имеющий доступ к общей
`moex-sentinel-data`, может обратиться к отдельному внутреннему API PROD Core;
обратное направление аналогично. TEST и PROD сохраняют отдельные Core API и
общую PostgreSQL/database. Analytics и TEST Worker не имеют прямого
присоединения к data network по этим файлам; их достижимость к PROD Core не
следует объявлять прямой. Вывод основан на статической конфигурации и не
является runtime attack proof.

Минимальное исправление: закрыть внутренние endpoints service-auth/ACL из S1 и
ограничить сетевую достижимость API/входа по контурам; оставить одну
PostgreSQL/database согласно согласованному решению. Если нужна более строгая
сетёвая граница, ограничить Core ingress и разделить client network segments,
не перенося и не дублируя базу.

### Important B1 — число уникальных инструментов может превысить лимит Analytics

`src/trading_automaton/usecases/broker_iteration.py:68–78` собирает все
уникальные `external_instrument_id` активных команд одного broker iteration и
передаёт их единым `AnalyticsSnapshotRequest` в fetch, затем в preparation.
Контракт Analytics ограничивает один запрос максимум 100 инструментами
(`src/sentinel_contracts/analytics.py:82–95`, поле `max_length=100` на строке 84).

Сценарий: 101 уникальный активный инструмент вызывает `ValidationError` при
построении запроса до fetch/preparation; preparation этого broker iteration
не выполняется. Команды накапливаются, так как authoritative admission cap не
обнаружен. Это не доказывает остановку task rebuild loop, уже отслеживаемых
команд, других brokers или outbox/control processing.

Минимальное исправление: обрабатывать набор партиями до 100 инструментов,
сохраняя TTL, generation, deduplication и recovery semantics, либо ввести
авторитетный admission limit с явной обработкой переполнения как продуктового
решения. Простое `[:100]` теряет инструменты и не является корректным решением.

### Minor D1 — архитектурный документ устарел по PROD read-only режиму

`docs/architecture.md:9,11,89–93` описывает текущую интеграцию только с sandbox
и утверждает, что исполняется только sandbox. Реализация при этом содержит
PROD read-only broker session: `src/trading_automaton/adapters/tinvest_broker_session.py:127,132–134`
вызывает `GetPositions` в PROD режиме. Документ может заставить сопровождающего
ошибочно считать, что PROD read-only путь отсутствует.

Минимальное исправление: уточнить назначение sandbox-only тезиса и описать
текущий ограниченный PROD read-only путь, сохранив ограничения на PROD TRADE.

## Завершённость пакета от 06.10

Историческая таблица и доказательства: [отчёт исправлений слоистой архитектуры
и SOLID](layered-solid-fixes-20261006.md); постановка и scope —
[`docs/superpowers/plans/2026-10-06-layered-solid-fixes.md`](../superpowers/plans/2026-10-06-layered-solid-fixes.md).
Проверка текущих source/test файлов выполнена по manifest из 448 путей: все
зафиксированные хеши совпали с историческим baseline. Все девять пунктов
закрыты по текущей реализации. Ниже приведены representative locations
текущего кода и тестов; подробные receipts остаются в историческом отчёте:

| Пункт | Текущий код | Текущие тесты | Результат сверки |
| --- | --- | --- | --- |
| I1 — missing broker | `src/moex_sentinel/domain/user_brokers.py:24` | `tests/api/test_missing_broker_contract.py:12` | Domain lookup error явно преобразуется в `BROKER_NOT_FOUND`; 404 без adapter construction. |
| I2 — stale decision admission | `src/trading_automaton/services/streaming_batch_tick.py:152`; `src/trading_automaton/storage/repository.py:1750` | `tests/trading_automaton/services/test_streaming_batch_tick_service.py:1007`; `tests/trading_automaton/storage/test_local_repository.py:577` | Admission до нового intent; committed/sent intent продолжает supervision; проверки входят в свежий backend run. |
| I3 — builder ownership | `src/trading_automaton/composition.py:271` | `tests/trading_automaton/test_streaming_composition.py:302` | Ошибка/отмена до передачи bundle освобождает принадлежащие builder ресурсы с сохранением первичной ошибки. |
| M1 — storage dependency | `src/moex_sentinel/domain/persistence_errors.py:4`; `src/trading_automaton/services/lot_ledger.py:13` | Worker/Core storage selections входят в полный backend run; ссылки на выборки — в историческом отчёте | Persistence errors и узкие storage contracts отделены от конкретных реализаций. |
| M2 — broker port | `src/moex_sentinel/services/ports.py:9` | `tests/services/test_broker_port_contract.py:17` | Lookup/read capabilities отделены от broker configuration writes. |
| M3 — vendor/transport semantics | `src/moex_sentinel/adapters/tinvest/request_errors.py:43`; `src/trading_automaton/services/uncertain_intent_reconciliation.py:235` | Проверки adapter/reconciliation входят в полный backend run; ссылки на выборки — в историческом отчёте | SDK/HTTP ошибки переводятся в нейтральные contracts; application policy не зависит от транспорта. |
| M4 — decision/batch contract | `src/trading_automaton/services/streaming_position_decision.py:34`; `src/trading_automaton/domain/dtos.py:242` | Проверки decision/batch входят в полный backend run; ссылки на выборки — в историческом отчёте | Явные DTO и committed result contracts используются вместо неявного чтения полей. |
| M5 — Analytics boundary | `src/trading_automaton/services/position_state_hydration.py:86` | `tests/trading_automaton/services/test_position_state_hydration_service.py:126` | Worker использует подготовленные Analytics metrics; удалённые local calculator/stream пути сверены по caller graph. |
| M6 — direction guards | `tests/test_architecture.py:127,278` | Negative import fixtures и whole-source gate прошли в свежем backend run | AST gate и negative fixtures проверяют запрещённые направления зависимостей. |

Очередь `docs/phase-1-implementation-plan.md:361–405` содержит эту завершённую
веху и следующие задачи. W3/C5 и broker editor остаются открытыми пунктами
очереди, но не являются незакрытыми частями старых I1–I3/M1–M6. Новый B1
capacity finding возник при проверке неограниченного набора команд и не меняет
историческое решение M4 по форме batch DTO.

## Свежие проверки и доказательства

Полный backend запуск на отдельной PostgreSQL завершился с **1991 passed, 0
failed, 0 skipped, 20 warnings за 192.06s**. XML содержит 187 PostgreSQL checks
и два paired-restore сценария. Тестовый экземпляр PostgreSQL 16.15 использовал
отдельный test volume и loopback port 55433; зафиксированный уровень изоляции —
`READ COMMITTED`. Это свежая локальная проверка приложения и тестового контура,
не гарантия production SQL isolation.

Product Ruff прошёл; Black подтвердил 448 файлов без изменений; `git diff
--check` прошёл на проверенном исходном пакете. Historical manifest подтвердил
448 неизменённых source/test файлов. Root broad Ruff сообщил 20 проблем в
`.codex/hooks`, broad Black — 17 файлов вне product scope (helpers/develop);
они не относятся к production source/test пакету и не включены в product
приёмку. Свежие receipts находятся в
[`develop/reports/isolation-completeness-20261008/`](../../develop/reports/isolation-completeness-20261008/):
`backend.xml`, `backend.log`, `verification-counts.json`, `ruff-product.log`,
`black-product.log`, `diff-check.log`, `historical-manifest-check.json`.
Полные команды и условия запуска приведены в
[`verification.md`](../../develop/reports/isolation-completeness-20261008/verification.md).
В частности, `BROKER_ACCESS_MODE=READ_ONLY` и `PYTHONPATH=src` задавались до
pytest collection; первый запуск без этой переменной завершился 23 collection
errors и не является итоговым PASS. Этот результат относится к указанному
backend запуску с заданной средой, а не к независимому standalone запуску
pytest из README.

После исправления test helper containment proof повторно запущен root:
`reproduce-findings.log` завершился exit 0, итог и hashes — в
`reproduce-result.json`. Актуальный TestClient показал `public 401`, `internal
200` и совпадение synthetic token; capacity checks дали `100 unique → fetch 1 /
preparation 1`, `101 commands / 1 unique → fetch 1 / preparation 1` и `101
unique → ValidationError до fetch/preparation`. Это подтверждает application
behavior и тестовый guard, но не сетевую достижимость из production контейнера. Отдельный
PostgreSQL post-inspection (`test-db-inspect.json`, `test-db-after.txt`)
зафиксировал healthy test instance, `READ COMMITTED`, ноль тестовых схем и
ноль восстановленных баз. `protected-files-check.json` подтверждает, что 757
защищённых файлов не изменились и index остался прежним.

## Оставшиеся ограничения и review

- Не проверялись VPS, развернутые compose-конфиги, фактические сетевые правила,
  фактические production DB grants/RLS и production transaction isolation.
- Frontend/browser tests не запускались: `node_modules` отсутствует. Source
  frontend не менялся в рассматриваемом пакете.
- Не проверялись производительность и восстановление при реальной нагрузке;
  тестовый PASS не задаёт latency SLA.
- Один PostgreSQL/database для TEST/PROD принят по scope; замечание S2 относится
  к доступу к API и сетевым контурам, а не требует отдельной базы.
- Архитектуру проверил `service_architecture_audit`, доступ и контуры —
  `service_security_audit`, завершённость и итоговую интеграцию — независимый
  `recent_completeness_audit` (все gpt-6.1-sol/high, read-only). Последний
  подтвердил кодовую матрицу девяти пунктов, S1/S2/B1, XML, хеши и сохранность
  index; самостоятельно тесты не запускал. Авторы helper и отчёта —
  `audit_evidence_worker` и `audit_report_writer` (gpt-6-luna/medium);
  root интегрировал результат и запускал проверки.
- Review качества итоговых материалов не выявил Critical/Important. Important
  в изоляции экспериментального helper исправлен и независимо перепроверен;
  Minor ссылки на контракт Analytics исправлен root. Это приёмка read-only
  аудита и его доказательств. Замечания приложения **S1/S2/B1 остаются открытыми**;
  общая изоляция и безопасность системы не объявляются прошедшими проверку.

## Запрошенная следующая проверка: TEST после релиза

08.10 пользователь сообщил, что в TEST не идёт торговля после релиза, а
последние операции датированы 29 сентября. Это наблюдение пользователя;
свежие данные сервера в этом аудите не получены. Проверка записана в CURRENT
и локальном плане как следующий шаг после предоставления SSH-доступа и
явного разрешения на диагностику TEST.

Историческая запись [релиза 04.10](../deployment/remote-compose.md) фиксирует
оба Worker, `READ_ONLY` и `STRATEGY_ENABLED=false`. Это кандидат причины
отсутствия новых сделок, который нужно сверить с текущим runtime. Диагностика
проверит версии, режим доступа, strategy flag, Worker/control/heartbeat,
состояния автоматов, свежесть рынка/Analytics, outbox и даты операций
Broker/Core/Worker относительно UI. SSH, deploy и переключение в TRADE
этим запросом на описание доступа не разрешены и не выполнялись.
