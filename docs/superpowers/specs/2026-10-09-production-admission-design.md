# Допуск PROD торговли перед первым запуском

Дата: 09.10.2026. Требование владельца: спроектировать, реализовать и развернуть
недостающие ограничения перед реальной торговлей. Основа — существующие
[production design](production-api-design.md) и [P3–P5 plan](../plans/production-api.md#p3-допуск-торговли-и-восстановление).

Уточнение владельца 09.10.2026 для rollout: применять проверенную миграцию
непосредственно к рабочей БД; после запуска проверить health/ping и базовый
интерфейс с read-only допуском DISARMED. Backup/restore rehearsal и полные
проверки целостности/финансовых графов не входят в актуальный DoD этого
deployment. Каждая миграция содержит проверенный безопасный rollback-скрипт.
Далее сохраняются контрактные требования к коду; прежнее требование paired
restore в разделе приёмки считается заменённым этим указанием владельца.

## Результат и границы

Core управляет единственным владельцем immutable broker/account scope,
операторским arm/revoke, финансовыми ограничениями и durable reservations.
Worker сохраняет SQLite execution/outbox и единолично отправляет RPC; Analytics
остаётся market-only. Новых зависимостей, транспорта, БД или UI не требуется.
Управление — существующая operator auth/Origin/CSRF, узкие API и CLI.

Релиз выкладывается PROD READ_ONLY/strategy=false/disarmed. Реальная активация
требует точного UID и численных caps владельца, принятой сверки P4 и отдельного
P5. Отсутствие этих значений не блокирует разработку/деплой, но запрещает arm.
Все денежные значения finite Decimal, все времена UTC millisecond.

Свежая база: HEAD62639a8, P1/P2 выполнены; семь guards закрывают PROD+TRADE.
09.10 10:58–11:04 МСК: TESTTRADE, PRODREAD_ONLY, Core/Worker fresh control,
TESTorders41/executions39, PRODorders/executions0, schema0003. Это снимок,
а не обещание будущего runtime. Source-references: Core api/app.py/internal
routers, Worker composition.py/order_dispatch.py/BrokerSdkSession,
uncertain_intent_reconciliation.py, storage/schema_revision.py.

## Доверие и идентичность

PROD Core использует отдельный INTERNAL_WORKER_TOKEN, привязанный к
INTERNAL_WORKER_ID и INTERNAL_WORKER_BROKER_ID. Worker на процесс создаёт
runtime_instance_id UUID и случайный 256-bit runtime_secret в wire-формате
64 lowercase hex (`secrets.token_hex(32)`). Core хранит digest,
никогда не возвращает/не логирует secret. Enrollment bearer разрешает только
регистрацию; повтор UUID с другим secret запрещён. Чувствительные internal
requests требуют bearer, X-Runtime-Instance-ID и X-Runtime-Secret; scope берётся из
проверенного principal, а не caller body. Token/secret — SecretStr в config,
validation/log/error output не раскрывает значения.
X-Runtime-Instance-ID постоянен на процесс и сверяется с runtime registration;
X-Process-ID остаётся отдельной необязательной корреляцией business process.

Защищены старые connection/commands/statuses/facts/heartbeat вместе с новыми
admission endpoints. Поддельный fact не должен изменять Core ledger/risk basis.
Market-only /internal/market сохраняет доступ Analytics без credentials.
TEST сохраняет свой протокол и environment guards; TEST Core не обслуживает
PROD scope. Общая S2 network hardening и миграция Worker storage вне задачи.

Credential/fact owner закреплён за account/instance/generation. Новый кандидат
может зарегистрироваться и наблюдаться без credentials/прав publish. Только
текущий owner получает credentials и пишет scoped facts; revoke оставляет ему
reads/reconciliation/outbox. При transfer старый instance retired и отвергается;
новый replay сохраняет исходные event UUID и sequence.
Только первая READ_ONLY registration scope, никогда прежде не имевшего owner,
назначает generation1/DISARMED и lease. Любая последующая registration остаётся
PENDING без credentials/publish, даже если прежний owner READ_ONLY или lease
истекла. Повтор того же UUID/secret идемпотентен, expiry не оживляет lease.
В первом deploy прежний PROD Worker чисто останавливается до registration.

## Владение, arm и controlled transfer

Core хранит owner_generation, lease expiry, arm revision/expiry, limits,
configuration fingerprint, принятые baseline/reconciliation.
Reconciliation — короткий операторский prerequisite для arm/recovery clear:
после свежего risk capture её proof действует до 30s. Успешный arm не истекает
вместе с этим proof: каждый submit всё равно требует новые authoritative reads
и согласованность position anchor. Arm expiry задаёт оператор явно, не позже
конца текущего московского дня; она может превышать текущий lease. Продление
lease не продлевает arm. Permit всегда ограничен действующей lease и arm expiry,
поэтому отдельные сроки не разрешают отправку без живого owner. Renewal разрешён
только тому же экземпляру до expiry. Expiry закрывает выдачу permits и переводит
в STOP_REQUIRED; renewal/restart/revoke не освобождает owner и не оживляет lease.

Transfer: revoke → остановка прежнего процесса и autorrestart → authenticated
operator attestation точных instance/generation/stopped_at/evidence_ref →
явное назначение зарегистрированного recovery owner → replay прежнего durable
outbox и свежая attributable reconciliation → явный свежий arm. Назначение
recovery owner атомарно retire прежний instance, повышает generation и закрывает
arm. Recovery owner получает credentials/reads/facts, но ни одного submit permit.
Unresolved reservations/active broker orders запрещают arm, а не передачу recovery
ownership: иначе новый экземпляр не смог бы доставить сохранённые доказательства.
OwnerStoppedRequest содержит candidate_runtime_instance_id; эта одна операция
атомарно принимает stop attestation и назначает recovery owner. Reconcile не
выбирает owner повторно. ReconciliationRequest: expected_revision, canary_uid
optional. Transfer требует принятую stop attestation.
Account-only reconciliation с UID null позволяет READ_ONLY recovery, но не arm.
Canary-ready reconciliation требует UID и authoritative position anchor; на первом
arm/после transfer нет активных ordinary/stop-orders/unresolved reservations. Attestation
доверяет оператору; Core не доказывает удалённую остановку и не fencing для брокера.

Arm требует конфигурацию TRADE с явно enabled strategy, live зарегистрированный
owner, текущую lease, свежую reconciliation того же canary UID/credentials,
принятую baseline текущего московского дня и все caps. CAS expected_revision
предотвращает потерю revoke/изменений. Изменение credentials/config закрывает
старый arm; submit также несёт fingerprint actual SDK bundle, чтобы re-arm с
новым токеном не разрешил отправку старым SDK session.

## Финальная граница отправки

Core authorize_submit проверяет principal и immutable request identity,
получает fresh authoritative broker data сохранёнными Core credentials, затем
в короткой DB transaction блокирует account row. Повторно сверяет owner,
generation/revision/lease/arm/config, TTL и текущие durable reservations;
атомарно проверяет caps, создаёт IN_FLIGHT reservation/permit/audit. Broker
awaits вне SQL transaction; SQL операция целиком через существующий run_sync.

Этот commit — граница «RPC потенциально летит». Revoke не обещает отменить
ранее выданное разрешение. После потерянного ACK/повтора consumed key нового
permit нет; reservation не исчезает по возрасту. Immutable fingerprint включает
scope, automation, UID, side, lots, price, idempotency key; изменённый payload
того же key отвергается. Denial различает доказанное отсутствие новой отправки
и возможное существующее in-flight состояние: ALREADY_CONSUMED не даёт ложный
CANCELLED/zero-fills.

PROD BrokerSdkSession требует SubmitAdmissionPort. Он вызывает gate прямо
перед post_order и проверяет fingerprint/deadline; между последней проверкой и
SDK нет другого await/audit/sleep. Срок permit — min(lease, arm, market deadline,
1s техническое окно от финального grant после fresh reads). Permit возвращает
server_response_at; Worker консервативно вычитает полный HTTP roundtrip из
expires_at−server_response_at и держит оставшийся срок монотонными часами.
Submit request также содержит submit_deadline, заданный Worker до HTTP, и Core
проверяет его вместе с market deadline перед consume: заведомо медленный сбор
данных не создаёт непригодный grant. Любой retry проходит gate заново;
неопределённый post автоматически не повторяется. Decision preparation также
читает effective admission/allowlist: disarmed/non-canary WAIT без intent.

Pre-commit known-no-submit denial: CANCELLED0, локальный reserve release, HOLD/reason.
Unknown authorization/post outcome: UNCERTAIN/HOLD, прежний key и reserve,
reads/outbox/reconciliation продолжаются. Core TInvestOrderExecutionAdapter
submit/cancel остаётся закрытым для PROD; builder для безопасного trading-status
создаёт этот adapter READ_ONLY даже при Core TRADE capability. Cancel живых
заявок не является неявным действием kill switch.

Успешный grant ACK с последующим окончательным отказом SDK gate ДО входа в
post_order отличается от неопределённой отправки. Только эта ветка создаёт typed
PreRpcAbortProof (permit_id, key, fingerprint, original runtime UUID/generation,
configuration fingerprint, abort time/reason). Proof атомарно сохраняется вместе
с локальным CANCELLED0 в существующем BrokerOrderStateChanged JSON outbox;
SQLite schema не меняется. Core ingress под account lock в той же fact transaction
проверяет exact consume, original owner history, authenticated current/recovery
owner, scope/fingerprint, zero broker ID/executed lots/executed amounts/fees и
proof (requested_amount=N сохраняется). Только такой consume
становится CONFIRMED_NOT_SENT; key навсегда consumed. Exact replay/потерянный fact
ACK идемпотентны, recovery owner доставляет original proof после stop/transfer.
Generic CANCELLED, lost grant ACK, post exception и crash до durable proof ничего
не освобождают и остаются UNKNOWN. Proof доверяет авторизованному Worker и его
строгой pre-post ветке; тот уже имеет broker credentials. После SDK entry proof
невозможен. Тесты проверяют ложный/mismatched proof и crash после durable proof.
Fresh risk reads с истёкшим техническим deadline отвергаются до consume; покрыть
медленный ответ, чтобы не выдавать заведомо непригодный grant. Точное окно и
clock handling сверяются тестами SDK gate, финансовые thresholds не подменяются.

## Финансовые определения

AdmissionLimits без defaults: canary_uid (ровно один), currency RUB,
max_order_notional>0, exposure_scope единственное CANARY_UID_GROSS,
max_exposure>0, max_outstanding_orders strict positive int, max_daily_loss>0.
Owner выбирает scope бюджета и принимает baseline policy; numeric values не
выбирает код. Поддержаны SHARE/FUND, RUB, long, целые торговые лоты. Неизвестные
currency/lot/type/market/commission/account data закрывают допуск.

N = lots × canonical_lot_size × limit_price. Core получает metadata сам,
проверяет UID против automation. Max-order действует на BUY, SELL_PART и
SELL_ALL; автоматического дробления/ликвидационного bypass нет. SELL limit
ограничивает requested notional, не абсолютную выручку при лучшем исполнении.
GetOrderPrice.initial_order_amount = N безfee; executed_commission = полная
предварительная комиссия, service/deal — её компоненты, не двойное сложение.
BUY reserve=N+fee; неизвестная комиссия запрещает submit.

GetPositions fresh: money−blocked, limits_loading_in_progress/negative/unsupported
margin/derivatives => DATA_UNKNOWN. Консервативный spendable cash = money−blocked−
all_unsettled_own_BUY_cash_reserves. Пока settlement не доказан, reserve вычитается
полностью, даже при уже отражённой брокером блокировке: возможен временный двойной
вычет, но mixed foreign blocked + pending reserve не завышает доступные деньги.
SELL проверяет unblocked long units минус все unsettled SELL units reservations.
Нет неттинга BUY/SELL до real fill.
Outstanding — union broker orders/stop-orders и durable unresolved reservations,
dedup лишь по доказанной request/order identity. UNKNOWN не исчезает из-за
пустого GetOrders. Foreign active ordinary/stop-orders запрещают каждый
authorize_submit, включая появившиеся после arm; первый arm также их запрещает.

CANARY_UID_GROSS: longunits × freshRUBmark (BUY projection не меньше limitmark).
Это бюджет алгоритма выбранного UID, включая имеющуюся позицию; account equity и
daily loss общесчётные. Общая экспозиция других UID вне этого canary бюджета.
Добавляются
ещё не отражённые BUY reserves; незавершённые SELL не уменьшают exposure.
Снижение/освобождение reserve только после attributable broker state и свежей
согласованной позиции. Core хранит position anchor и cumulative attributable
fills; внешнее изменение/несогласованность закрывает допуск. Один Worker fact
не освобождает budget; частичный fill/CANCELLED сохраняет inventory и fees.

Daily loss — decline account RUB equity от ПЕРВОЙ ПРИНЯТОЙ baseline московского
календарного дня, с поправкой на external cash flows. Это явно не полный P&L от
полуночи при позднем принятии baseline. Unique(scope, Moscow day), immutable
baseline не сбрасывается restart/revoke/re-arm; новый день закрывает arm.
Owner принимает определение при baseline/arm. Формула:
max(0, baseline_equity + net_external_RUB_cash_flow − fresh_current_equity).
Equity=GetPortfolio(currency=RUB).total_amount_portfolio; expected_yield — процент,
не денежная loss база. Core полностью проходит GetOperationsByCursor между
baseline/current snapshot; неизвестный transfer/currency, незаконченная пагинация,
повтор cursor, неоднозначность внутри capture window => DATA_UNKNOWN.
`capture_started_at` предшествует независимым account/risk reads;
`captured_at` — фиксированный cutoff после завершения всех non-cursor reads.
Cursor читается затем полностью с постоянным `to=captured_at`;
`operations_through=captured_at` допустим только после terminal page.
Любой external INPUT/OUTPUT внутри включительного capture window закрывает
baseline/current snapshot как неоднозначный. Переводы после cutoff относятся
следующему snapshot, поскольку account reads уже завершены. Срок risk данных
`captured_at+1s`, а freshness проверяется после cursor drain и закрытия клиента;
медленная пагинация не продлевает этот срок.
Комиссии/торговые payment повторно из equity не вычитать. Capture provenance,
actor и policy сохранены. При loss threshold/инциденте состояние закрыто;
простой повторный arm не обнуляет loss.

## Контракты API и хранения

Shared source: sentinel_contracts/trading_admission.py. PositionalModel frozen,
extra forbid; strict units/UTC/finite decimals. RuntimeRegistrationRequest/result,
AdmissionLimits/View/Baseline/Reconciliation, Arm/Revoke/OwnerStopped requests,
SubmitAuthorizationRequest/Permit/Denied и reason enum.
Submit request: broker/account/automation/key, generation/revision,
configuration_fingerprint, UID/side/lots/price/market_valid_until/submit_deadline. Response bound
fingerprint/generation/revision/deadline/risk_snapshot_id; denial содержит
outcome_uncertain. BrokerConnection получает optional configuration_fingerprint;
DispatchRequest additive generation/revision fields сохраняют TEST defaults.
Reconcile request содержит optional canary_uid и expected_revision;
OwnerStoppedRequest — candidate_runtime_instance_id. Shared PreRpcAbortProof применяется к additive optional
BrokerOrderState и BrokerOrderStateChangedPayload; расположить модель без cyclic
imports и сохранить старые TEST JSON defaults. OnlyCore author shared models.

Internal: POST /internal/automaton/runtime-instances; GET admission;
POST admission/renew; POST submit-authorizations под brokers/{broker_id}.
Operator: GET /api/brokers/{broker_id}/trading-admission; POST baselines,
reconcile, arm, revoke, owner-stopped. Auth session actor, exact Origin+CSRF;
invalid422, CAS409, auth401/403, submit business denial typed200. Secrets скрыты.
Ни один raw caller финансовый total не считается authoritative broker data.

0004_trading_admission additive после0003: worker_runtime_instances,
trading_account_admissions, trading_admission_baselines,
trading_submit_reservations, trading_admission_events. Immutable unique account
scope/key/day, foreign keys/constraints, safeaudit. Reservation states IN_FLIGHT,
BROKER_ACTIVE, UNKNOWN, SETTLED, CONFIRMED_NOT_SENT; unknown не TTL-delete.
Downgrade при непустых durable таблицах запрещён под блокировками. Worker
SQLite schema не меняется без доказанной нужды; instance secret volatile.

## PROD recovery и совместимость

В SDK1.49.3 GetOrderState поддерживает OrderIdType.ORDER_ID_TYPE_REQUEST.
PROD find_by_idempotency_key запрашивает этот key и проверяет order_request_id.
NOT_FOUND/mismatch/unsupported оставляет UNKNOWN/HOLD/reserve. PROD эвристика
_state_from_operations отключена; TEST fallback сохраняется. Никаких fabricated
execution facts из похожей внешней операции/portfolio delta.
Retained CREATED/DISPATCH_PENDING/SUBMITTING/SUBMITTED/ACCEPTED/PARTIALLY_FILLED
после restart не пропускаются: PROD recovery выполняет read-only request-key
lookup и attributable finalize ровно один раз, никогда повторный post. При
неизвестном исходе UNCERTAIN/HOLD/reserve. Отмена definitely-never-granted очереди
допустима лишь по доказанному Core consume state; отсутствие строки в одном
неуспешном запросе не является доказательством. Crash после broker accept до
local persist и cancellation polling до terminal покрыты recovery tests.
Read-only recovery — характеристика операций, не смена static capability:
при retained active SQLite intents Worker запускается в прежнем TRADE capability,
но Core owner RECOVERY_ONLY/DISARMED и SDK admission не допускают mutations.
Существующие READ_ONLY+active-intents guards сохраняются. Default READ_ONLY rollout
проверяет пустую active очередь. SubmitAttemptView по ключу возвращает durable
consume или доказанное отсутствие grant только для уже retired generation;
пока исходный owner может завершить authorize, отсутствие строки не доказывает
неотправку. Временное отсутствие ответа также не является доказательством.

Exact schema head остаётся строгим. Shared0004 требует обновления TESTCore и
collector совместимым artifact; TESTWorker остаётся прежним, если additive
protocol не требует изменений. Не удалять volumes и не возвращать БД до уже
совершённых сделок. Rollbackcode только совместим с0004. Новый runtime registry
не должен удалить/обнулить прежний outbox, bootstrap и финансовые связи.

## Приёмка и источники

Обязательны реальные isolated PostgreSQL tests: rowlock конкурентных budgets,
expiry/pause WorkerA→WorkerB denied, revoke before/after grant, lostACK/reservations,
partialfill/replay/correction/restart, broker-attributed recovery vs похожая
внешняя операция, dailybaseline/cashflow/midnight/unknown, forgedoldinternalfacts,
immutable scope, secondadapterbypass, 0003→0004 preservation+nonemptydowngrade
и TESTregression как проверки контрактов кода на отдельной БД.
Paired PG+SQLite restore — историческое требование первоначального rollout,
исключённое указанием владельца из текущего deployment DoD.
Default/missingcaps/strategy/arm/UID =>
zero broker mutations. Независимый reviewer без Critical/Important до rollout.

Authoritative contracts: [Orders](https://developer.tbank.ru/invest/services/orders/methods),
[Operations](https://developer.tbank.ru/invest/services/operations/methods),
[GetOrderState](https://developer.tbank.ru/invest/api/orders-service-get-order-state),
installed SDK1.49.3 async_services.py. Проектные численные caps и конкретный UID
владелец ещё не задал; модель валидирует, но deploy остаётся disarmed.

09.10 уточнение economic identity по integration review: submit request и
request fingerprint включают ожидаемый размер лота и валюту Worker. Core
сверяет их с текущими каноническими metadata под блокировкой до consume.
Устаревшие metadata закрывают отправку без создания резерва; сумма
pre-RPC proof по-прежнему должна точно совпадать с сохранённым consume.
