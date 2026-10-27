# Task2 review package

Current checkout f751ab3 with protected dirtybaseline582files. Diff against pre-task bytes, no commits.

## docs/deployment/uncertain-order-investigation.md
SHA256 925a25567fb41e4fa63a44ab8683831d51a9e51972979e8bcf0dec18a71c1c63
```diff
--- baseline/docs/deployment/uncertain-order-investigation.md
+++ current/docs/deployment/uncertain-order-investigation.md
@@ -0,0 +1,78 @@
+# Разбор неопределённой заявки после D1
+
+Этот runbook задаёт read-only процедуру сбора доказательств для одной заявки в состоянии `UNCERTAIN`. Он не устанавливает причину текущих GOLD/SIBN и не разрешает менять состояние автомата. По аудиту 20.10.2026 известны `HOLD/UNCERTAIN` без терминального факта, но доступные логи `GetOrderState/NOT_FOUND` не связаны с исходными request ID. Причина пока не доказана.
+
+## Предусловия и границы
+
+1. D1 должна быть реализована и отдельно развёрнута на совместимых Core и Worker. Проверить версии и capability marker `execution_diagnostics='v1'` в `GET /internal/runtime`. HTTP 404 на новом маршруте неоднозначен: возможны старая версия, отсутствующая automation или отказ scope. Сверить версию/capability и штатный scope, затем остановиться, если маршрут недоступен; не обходить это SQL, SQLite, старым API или прямым доступом к хранилищу.
+2. Новый D1 маршрут — `GET /api/trading-automations/{automation_id}/execution-diagnostics?limit=20&intent_id=<optionalUUID>`; `limit` от 1 до 20, `intent_id` необязателен. Начать с GET без `intent_id`, выбрать точный active intent UUID из scoped ответа и выполнить focused GET с этим UUID. Если подходящего intent в ограниченной/усечённой проекции нет, не угадывать UUID и не объявлять отсутствие заявки доказанным.
+3. Это один Core HTTP маршрут, который возвращает Core persisted facts и последнюю полученную cached Worker projection. Поэтому HTTP request/capture time Core, Worker projection source `captured_at` и receipt time в Core записывать раздельно; instance/generation источника также сохранять. Повторный GET может вернуть ту же Worker projection и не является новым Worker observation. Если source capture/receipt не изменились, отметить projection unchanged, проверить TTL и прекратить polling после конечного числа попыток/бюджета, согласованного оператором; не считать повторы новыми наблюдениями.
+4. Считать Worker-проекцию пригодной только при `observation=OBSERVED`, правильных broker/automation/account/instrument и owner identity, свежих source/receipt timestamps (TTL 30 секунд, допустимое будущее смещение не более 5 секунд) и явном статусе полноты. Takeover, устаревшая проекция, `UNKNOWN`, truncation, ошибка чтения или неизвестная причина делают отсутствие строки недоказательным. Core accepted envelope не доказывает получение ACK Worker; без отдельного доказательства доставки/ACK указывать `ACK_UNKNOWN`.
+5. Собирать только идентификаторы, состояния и безопасные числовые/временные поля. Не сохранять credentials, raw payload, strategy snapshot, exception text, токены или полные SDK-ответы. Перед распространением редактировать account и broker IDs до стабильных локальных псевдонимов; request key и broker order ID оставлять только в защищённом локальном рабочем материале, когда это нужно для точной корреляции.
+6. Не выполнять POST, cancel, Resume, изменение owner/lease, ручную очистку резерва, SQL/SQLite-запись или повторную отправку. Не запускать примеры GET или SDK lookup из runbook автоматически.
+
+## Срезы Core и Worker
+
+Сначала зафиксировать UTC capture time и runtime identity. Затем прочитать Core:
+
+```javascript
+// READONLY illustration — выполнить вручную как same-origin GET только после D1 deployment.
+const core = await fetch(
+  `/api/trading-automations/${automationId}/execution-diagnostics?limit=20`,
+  { method: "GET", cache: "no-store", credentials: "same-origin" }
+).then(async response => ({ status: response.status, body: await response.json() }));
+// Выбрать точный active intent UUID из scoped ответа; затем запросить его отдельно.
+const focused = await fetch(
+  `/api/trading-automations/${automationId}/execution-diagnostics?limit=20&intent_id=${intentId}`,
+  { method: "GET", cache: "no-store", credentials: "same-origin" }
+).then(async response => ({ status: response.status, body: await response.json() }));
+```
+
+Это только иллюстрация двух same-origin GET, её не исполнять в рамках этого runbook. `automationId` должен быть локальным UUID из разрешённого scope; `intentId` брать только из первого ответа. Не вставлять bearer token в URL, консольную команду или журнал. HTTP 404 сам по себе не определяет причину: сверить deployment capability и scope, а при неразрешённой неоднозначности остановиться; никакой обходной запрос к базе не допускается.
+
+Сохранить результат первого unfiltered Core GET и второго filtered Core GET отдельно, включая HTTP request/capture time. В Core-срезе записать persisted order, submit attempt и его request key, известный broker order ID, события, executions, reservation, owner, времена записи и статус/ошибку чтения. Для Core acceptance нужны конкретные envelope по `fact_execution_id` с event/sequence/received timestamp. `outbox=0`, пустая очередь, cached sequence, heartbeat или Core accepted envelope сами по себе не доказывают Worker ACK; при отсутствии отдельного подтверждения выставить `ACK_UNKNOWN`.
+
+Worker-часть обоих ответов — cached projection, присланная heartbeat-путём; это не отдельный HTTP опрос Worker. Проверять Worker `source captured_at`, Core receipt time, origin runtime/generation, `history_truncated`, pending/failed fact counts, intent state, side, quantity, price, requested/executed amounts, fees/currency, execution fact ID и emission time, created/updated/dispatch/responded/executed/terminal timestamps. Поля могут отсутствовать в пределах контракта — отсутствие при неполном/устаревшем срезе не равно отрицательному факту. Если повторный Core GET не принёс новый Worker source capture, так и записать.
+
+Для каждого intent построить одну цепочку, не склеивая соседние заявки по тикеру или времени:
+
+`Worker intent UUID → exact idempotency request key → Core submit attempt → broker response/requested lookup → известный broker order ID → broker state/fills → Worker durable execution и execution fact → Core accepted envelope → Worker ACK, если он отдельно доказан`.
+
+Совпадение ключа, счёта, инструмента, стороны и количества обязательно. Если один из обязательных идентификаторов расходится, связь остаётся неподтверждённой. Точные исходный dispatch и ответ SDK важнее общего сообщения `NOT_FOUND`.
+
+## Read-only проверки у брокера
+
+Если доступен штатный защищённый операторский read-only клиент и проверены account/environment, можно запросить состояние по точному исходному request key тем же способом, которым Worker выполняет lookup: `GetOrderState` с `ORDER_ID_TYPE_REQUEST`. Сохранить время запроса/ответа, account scope, тип идентификатора, безопасный статус, поля идентичности и fills. Не подменять его эвристическим поиском по похожим полям.
+
+Известный broker order ID можно проверять отдельным read-only `GetOrderState` только если его связь с тем же intent доказана Core submit response, Worker response или иной durable корреляцией. Указывать broker-ID тип согласно официальному SDK-контракту. Это дополнительный диагностический запрос, а не автоматический fallback и не разрешение на повторный post. Если identity/тип ID не подтверждён, lookup не выполнять и отметить пробел.
+
+`NOT_FOUND`, `UNIMPLEMENTED`, пустой ответ, недоступность истории старше поддерживаемого периода или несовпадение request key не доказывают, что отправки не было. Не переходить к `GetOrders` для эвристического сопоставления, не искать по инструменту/цене/времени как по идентичности и не использовать новый post для проверки. Не утверждать отсутствие торговли за день по счётчику 401/500 API-строк или одному `source_exhausted` продолжению истории операций: полноту устанавливает только полный контрактный cursor/date coverage D3.
+
+## Read-only история операций D3
+
+D3 маршрут: `GET /api/operations/scoped?broker_id=<str>&account_id=<str>&from_at=<awareRFC3339>&to_at=<awareRFC3339>&limit=100&cursor=<optionalopaque>`. Для каждой страницы повторять тот же broker/account/date interval, передавать `next_cursor` как следующий `cursor`, а первую страницу запрашивать без cursor. Интервал timezone-aware, нормализуется в UTC; контракт сравнивает `(from_at, to_at]` после согласованной миллисекундной нормализации. `limit` от 1 до 500; cursor opaque, не декодировать и не менять.
+
+Сохранять на каждой странице `input_cursor`, `next_cursor`, `source_item_count`, `selected_item_count`, `source_exhausted`, `coverage_complete` и `errors` вместе со всеми возвращёнными rows и временем ответа. API возвращает все подходящие rows исходной страницы, не обрезая их до selected `limit`. Межстраничный клиент ведёт seen operation IDs, дедуплицирует по `operation_id` и контролирует повтор cursor; остановиться с неполным coverage при повторе cursor/цикле, ошибке, invalidity, truncation или исчерпании конечного page/time budget. Не выдумывать числовой budget: зафиксировать выбранный оператором предел до запроса.
+
+`source_exhausted=true` означает только успешную текущую страницу без `next_cursor`. `coverage_complete=true` допустим только для запроса с `input_cursor=None`, при полном cursor chain, исчерпанном source и без ошибок/invalidity. У одной последней continuation страницы `coverage_complete=false` для покрытия всего интервала; также не считать coverage полным при `source_exhausted=null`, пропущенных страницах, повторном cursor или conflicting duplicate. Один `source_exhausted` и 401/500 строк в старом API не доказывают отсутствие торговли за день. D3 GET остаётся чтением истории и не заменяет per-intent корреляцию Core/Worker.
+
+## Классификация результата
+
+Выбрать ровно один результат и перечислить ссылки на Core request/capture срезы, отдельные Worker source/receipt timestamps и безопасные SDK-ответы:
+
+- **`terminalfacts`** — одна и та же заявка однозначно коррелирована, есть брокерский терминальный state/fills и согласованный durable Worker execution. Указать, дошёл ли соответствующий execution fact до Core и подтверждён ли Worker ACK. Сам терминальный факт не разрешает Resume, cancel, SQL или снятие резерва.
+- **`durablepreRPCaborted`** — durable запись однозначно показывает, что операция остановилась до RPC dispatch, и нет факта/ответа, указывающего на вызов брокера. Если dispatch неизвестен или есть таймаут/потеря ответа, этот outcome не подходит.
+- **`contradictory`** — источники расходятся по request key, broker ID, account, owner, execution/fills, состоянию или времени так, что одна цепочка не получается. Сохранить каждое противоречие, не выбирать удобный источник.
+- **`insufficient`** — любой существенный источник неизвестен, stale, не scoped, неполон, срез усечён, D1 несовместима или broker lookup не поддержан. Указать ровно недостающие поля и нужный следующий read-only источник.
+
+Во всех четырёх случаях при неустранённой неопределённости сохраняются `UNCERTAIN`, текущий резерв и блокировка повторной отправки. Никакой исход этого runbook сам по себе не меняет автомат.
+
+## После установления причины
+
+Если срезы доказывают конкретный дефект кода, оформить отдельный узкий fix с независимым review и RED→GREEN воспроизведением на изолированной среде. Сценарий должен соответствовать найденной причине и проверять границы потерянного response, потерянного ACK, partial fill, replay или `NOT_FOUND` только когда они относятся к доказанному дефекту. Проверить отсутствие двойного исполнения и сохранение reservation при неизвестном исходе. БД проверки выполнять только на отдельной тестовой БД.
+
+Если дефект не установлен, не добавлять fallback, автоматическую очистку или принудительное восстановление. Операционное действие возможно только после отдельной проверки его контракта и отдельного разрешения владельца. Снятие HOLD/резерва и ручные Resume/cancel/SQL не входят в эту процедуру. Документ не заменяет deployment D1, read-only runtime evidence, независимую проверку причины или приёмку исправления.
+
+## Основание и ограничения
+
+Контракт API и источников: [design «Диагностика исполнения и история операций»](../superpowers/specs/2026-10-20-execution-diagnostics-and-history.md), D1 и условная D2. Контекст 20.10.2026: [audit findings](../../develop/.planning/2026-10-20-application-audit/findings.md) и [progress](../../develop/.planning/2026-10-20-application-audit/progress.md). Историческое исследование `GetOrderState/NOT_FOUND`: `develop/reports/broker-not-found-20261019/report.md`; оно не является свежим runtime-срезом и не устанавливает происхождение текущих intents. Тест `test_prod_recovery_looks_up_exact_request_id_without_listing_orders` подтверждает lookup именно по request ID; этот факт не доказывает причину живого `UNCERTAIN` и не обосновывает fallback по broker ID.
```
