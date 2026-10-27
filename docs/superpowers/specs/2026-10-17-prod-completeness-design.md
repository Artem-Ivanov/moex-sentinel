# Полнота PROD-пути: архитектурная спецификация

Дата: 17.10.2026. Статический аудит исходников и тестов: [prod-completeness-20261017.md](../../audits/prod-completeness-20261017.md). Постановка уточняет согласованный владельцем дизайн. Это контракт для последующего плана и реализации; реализация и runtime-проверки этой спецификацией не заявляются.

## Цель и границы

Закрыть конкретные пробелы реального PROD-пути: операторскую изоляцию окружений, достоверность API trade availability, видимость постоянной блокировки Worker, ограниченный startup alert, истечение срока активного ордера с безопасным cancel/recovery и одну интеграционную проверку полной PROD-композиции. Существующие TEST/Sandbox, моки и тестовые контуры сохранить. Исправлять только PROD-поведение и контрактные места, необходимые для него.

Границы владения остаются прежними: Core владеет PostgreSQL, admission, operator API и подтверждёнными финансовыми фактами; Worker владеет SQLite, durable intent/outbox, отправкой и восстановлением broker orders; Analytics остаётся market-only и не получает broker credentials или портфель. Core не начинает отправлять/отменять PROD-заявки. Новая БД, транспорт, polling subsystem, schema migration или общий retry/reprice механизм не являются целью.

## Существующие гарантии, которые сохраняются

Core baseline/reconcile уже проверяют scope. Core risk повторно читает authoritative instrument flag при выдаче submit permit. PROD TRADE integration flow уже проверяет admission, expiry, tracking/recovery, replay и settlement; ingress-тест использует file SQLite Worker, Core HTTP/PG, потерянный ACK и runtime transfer. Эти пути дополняются точечными тестами, а не объявляются отсутствующими и не заменяются TEST-тестами. Отсутствие Analytics frame (`None`) не означает постоянной поломки broker runtime: recovery preparation продолжается.

Статический аудит не подтверждает состояние сервисов, брокера, PostgreSQL/SQLite или VPS. Текущее наблюдение и ограничения rollout описаны в локальном `develop/CURRENT.md`; исторические receipts не являются текущим deployment gate.

## Контракты поведения

### Операторские команды и контур

Все operator methods Core используют уже существующий `AutomatonBrokerService.scope` для проверки broker, выбранного account и environment. Аутентификация оператора остаётся отдельной существующей HTTP-проверкой. Включая команды и чтение pending runtime, ни один операторский endpoint не получает право из отсутствия или наличия broker credentials. Composition внедряет pinned `settings.application_environment` в сервис/репозиторий; репозиторий выполняет transactional guard, сверяя `broker.environment` с environment запроса/операции до любой мутации. Это закрывает гонку между ранней HTTP-проверкой и записью.

TEST operator, пытающийся изменить PROD row, получает стандартный отказ окружения `EnvironmentMismatchError` (`ValueError`), без изменения состояния и без 500. Проверки применяются ко всем методам, включая будущие/ожидающие runtime-команды. Существующие auth, ownership, CSRF/Origin и error mapping остаются обязательными. Baseline и reconcile сохраняют свою уже имеющуюся scoped защиту; расширение их не нужно.

Внутренние Worker-пути остаются отдельным контрактом: principal определяется действующей worker identity/runtime fencing, а не операторским scope или caller-supplied environment. TEST и PROD не смешиваются.

### Достоверность `api_trade_available`

TradingStatus stream-события не несут API trade availability. Поэтому stream adapter удаляет буквальное `api_trade_available=True` как неподтверждённое наблюдение. Core получает это поле из read-only `GetTradingStatusResponse` через существующий market-data source и ограниченный gateway refresh; отдельный периодический polling subsystem не создаётся. Официальный контракт: [T-Invest MarketData](https://developer.tbank.ru/invest/services/quotes/marketdata).

Для unary наблюдения Core хранит собственные `observed_at` и срок годности. Stream-событие не обновляет время и свежесть unary API flag. Отсутствующее или просроченное значение означает неизвестное состояние и закрывает market/decision eligibility. Подтверждённое `False` означает обычную недоступность инструмента, а не transport incident. Существующая проверка свежего authoritative флага в Core permit/risk path остаётся последней границей; сигнал Analytics не заменяет её.

### Broker runtime diagnostics

Worker diagnostics публикуют volatile состояние broker runtime: время/результат начала и завершения работы и безопасную причину блокировки. Если команды ещё не было — `IDLE`; если значение отсутствует или устарело — Core показывает `UNKNOWN`, не зелёное состояние. Permanent broker error явно отражается как блокировка. Секреты, токены, запросы и чувствительные broker payload не попадают в причину.

Heartbeat и control-plane progress по-прежнему проходят через WorkerVersionStore в Core diagnostics и существующий Telegram alert path, но не доказывают broker readiness и не разрешают submit. При постоянной ошибке диагностика должна стать DEGRADED с причиной, хотя control progress продолжается. Если Analytics недоступна и кадр равен `None`, recovery preparation продолжается; отсутствие кадра не должно переводить broker runtime в постоянный stop.

Core health, readiness и DB/schema health не становятся DOWN только из-за broker block. Новые diagnostics должны совместно раскатываться Core/Worker/UI: Core, получивший старый payload, трактует отсутствующее поле как `UNKNOWN`; Worker не должен выпускать состояние, которое старая строгая (`extra=forbid`) модель молча принимает за зелёное. UI показывает различие control progress и broker runtime.

### Startup alert

Для длительной начальной блокировки допускается alert только когда ожидается работа: scoped automation `IN_WORK`, конфигурация `TRADE`, strategy enabled, и причина входит в явный allowlist блокирующих причин. Применяются grace/confirmation period, deduplication и ограниченная отправка существующей очередью. Намеренные состояния `HOLD`, `READ_ONLY` и disabled исключаются. Пользовательское ручное разрешение или новый режим активации не вводится. Настройка приватных Telegram credentials/ENV не входит в задачу.

### TTL, cancel и durable execution

Deadline заявки равен `dispatch_started_at + order_ttl_seconds` из durable snapshot исходного intent. Он фиксируется один раз; перезапуск, смена ENV или восстановление watcher не продлевают его. Сравнение — UTC, миллисекундная точность, истечение при `now >= deadline`. Если snapshot, timestamp или proof отсутствует, Worker не изобретает значения и действует fail-closed. В частности, legacy intent без `dispatch_started_at` или валидного snapshot не получает TTL из нового ENV: срок не вычисляется заново и не продлевается, а intent остаётся в безопасном recovery/HOLD до достаточного proof.

Перед cancel Worker получает свежий status по тому же известному broker order ID и account/broker scope. Уже terminal ордер не отменяется. Для подтверждённого nonterminal ордера в активном `TRADE`, при текущем owner и действующем runtime fencing, разрешён scoped cancel. Это операция снижения риска: она не требует нового permit на ордер и не расходует admission caps. В `READ_ONLY` cancel запрещён. Если доказано, что RPC submit ещё не вызывался, истёкший intent проходит существующий pre-RPC abort; fabricated broker order или cancel не создаются.

ACK на cancel подтверждает только приём команды и метаданные времени, не terminal outcome ([официальный cancel contract](https://developer.tbank.ru/invest/services/orders/methods)). После ACK Worker запрашивает status и подтверждает terminal state в ограниченном бюджете, используя существующее окно polling. Timeout/lost ACK ведёт к запросу статуса того же order ID; повтор cancel допускается только после свежего подтверждения того же nonterminal статуса и с существующим cooldown. Если submit ACK потерян — поиск по исходному request ID обязателен; `not found` сам по себе не доказывает, что заявки нет, и не разрешает повторный submit. Лимит подтверждения исчерпан или исход неизвестен — состояние `UNCERTAIN`, automation переводится/остаётся в `HOLD`, новый submit запрещён.

Active intent и полный reservation сохраняются до атомарного `finalize_execution` вместе с durable outbox commit. Только после commit снимается gate и освобождается reservation; durable intent и финансовые факты сохраняются. При гонке fill/cancel учитываются реальные fills, их фактическая цена и комиссия, включая partial fill. Контракт broker/proof/local ledger определяет данные, необходимые для конкретной terminal финализации: при fills портфель загружается, когда он нужен существующей проверке fills и settlement; cancel без fills не должен получать blanket portfolio prerequisite. Недоступность портфеля не мешает scoped status query/cancel по известному order ID. Неполные доказательства не освобождают reservation.

На один intent существует один lifecycle watcher/recovery owner. После рестарта он продолжает исходный deadline. Обычный подтверждённый cancel сохраняет текущий `IN_WORK`; ранее разрешённое свежее и независимое strategy decision может создать новый intent с новым permit и проверкой общих лимитов. Это не автоматическая замена заявки по старому решению: его цена, количество и permit не переиспользуются. Запрещены цепочки retry, repricing и blind replacement старого решения.

`HOLD` — монотонная операторская граница для данного recovery race: lifecycle не переводит automation из `HOLD` в `IN_WORK`; ручной HOLD сохраняется при гонке и рестарте. Даже если позднее `UNCERTAIN` разрешён terminal фактом, automation остаётся в HOLD до штатного Resume. Финансовая история и unresolved UNCERTAIN не очищаются.

Неиспользуемый `STRATEGY_ORDER_RETRY_LIMIT` явно помечается deprecated и не получает нового значения/семантики. Исторические snapshot продолжают декодироваться, совместимость существующей конфигурации сохраняется. Перед реализацией необходимо проверить pinned SDK `t-tech-investments1.49.3`: точную сигнатуру cancel и способ указания order ID type нельзя выводить только из актуальной документации. Смена сигнатуры, несовместимая с pinned SDK, требует отдельного согласования пакета/зависимости.

## Проверки и критерии приёмки

Интеграционные проверки PostgreSQL запускаются только на отдельном тестовом экземпляре через `POSTGRES_TEST_DATABASE_URL`; создание схемы `test_<uuid>` не делает безопасным URL рабочей БД. SQLite проверки используют `tmp_path`. Существующие PROD admission flow/ingress сценарии сохраняются и расширяются только для недостающих взаимодействий. Нужен один интеграционный путь Analytics HTTP → production decision runtime → SDK boundary → Core HTTP/PG без подмены внутренних risk/admission и без реальной заявки.

Поведенческая приёмка должна покрыть: cross-environment operator rejection без мутаций; cancel denial при stale owner/generation, истёкшей lease и `READ_ONLY`; legacy intent без `dispatch_started_at` или валидного snapshot без TTL из нового ENV; stream без unary flag, stale/unknown/True/False и закрытие eligibility; permanent runtime error при живом control heartbeat и DEGRADED diagnostics; startup alert с исключением HOLD/READ_ONLY/disabled; TTL ровно на границе; fill до cancel и partial fill в гонке с cancel; ACK без terminal proof; timeout/lost cancel ACK; lost submit ACK и `not found`; crash/replay около atomic execution+outbox commit без duplicate resubmit; сохранение HOLD; восстановление с исходным deadline; недоступный portfolio при доступном scoped query/cancel. Запуск тестов и результаты будут зафиксированы на этапе реализации и не подразумеваются этой спецификацией.

## Rollout и совместимость

Rollout возможен после отдельного утверждённого implementation plan, достаточных целевых проверок, независимого C0/I0 review и сборки из точно проверенного immutable source/package. Пользовательское разрешение deployment существует, но не разрешает SQL/SQLite проверки в PROD, изменение приватного ENV, caps, ARM/Resume/HOLD, потерю volumes или действия TRADE сверх утверждённых gates. Известный до rollout UNCERTAIN intent должен быть сохранён. Устаревший package receipt не является gate; требуется свежая разрешённая application-level evidence для корректной работы с intent. SQL/SQLite PROD checks не использовать и freshness evidence не выдумывать.

Новый protocol payload должен переживать смешанный rollout без silent green: сначала Core/UI, понимающие отсутствие нового поля как UNKNOWN, затем Worker, который его публикует; либо другой доказанно совместимый порядок без окна ложной готовности. После deployment ограниченная проверка — health checks, ping сервисов и открытие UI с базовым функционалом. Если реализация обнаружит необходимость миграции, она должна включать рабочий downgrade, проверяемый на отдельной тестовой БД; миграцию не добавлять без необходимости. Безопасность downgrade после новых данных должна быть явной.

## Отклонённые варианты и открытая внешняя проверка

Не заменять TEST/Sandbox/моки удалением PROD-условий по текстовому поиску; не считать heartbeat broker readiness; не считать cancel ACK terminal; не снимать reservation на отправке cancel; не повторять submit после неопределённого ответа; не вводить новый broker polling service, manual approval layer или retry/reprice policy. Эти варианты либо дают ложную готовность, либо создают риск дублирования заявки и потери финансовой фиксации.

Перед реализацией остаётся подтвердить по локально установленному `t-tech-investments1.49.3` точную cancel signature/поля. Ни live credentials, ни брокерский ответ, ни фактическое состояние deployment этим статическим документом не подтверждены.
