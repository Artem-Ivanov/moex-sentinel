# Возобновить PROD trading — 18.10.2026

## Проверка актуальности до декомпозиции
Пользователь отменил относящиеся к другому проекту референсы и поручил «добить трейдинг». Scope — продолжение настройки и диагностики существующего PROD, без нового редизайна или повторного deployment здорового релиза.
Завершено: полнота PROD реализована, проверена и выложена `vps-20261018-prod-437cc0757b90`, owner generation 6. Источники: develop/CURRENT.md; docs/phase-1-implementation-plan.md, раздел 17–18.10; docs/deployment/prod-trading-start.md, фактический rollout18.10; acceptance-receipt.json в develop/reports/prod-completeness-20261018/.
Недостающее: свежая runtime-проверка после rollout; при transfer `limits=null`, историческая UNCERTAIN не перепроверена. Предыдущие health и caps — исторические, свежего разрешения/исполнения не доказывают.
Договорённости: ранее пользователь утвердил формулу caps (develop/WORK_LOG.md, 10.10 08:36:47UTC; develop/reports/prod-trading-enable-20261010/approved-limits.json); 16.10 её фактическое применение подтверждено в prod-limits-arm-20261016/report.md. Формула: max_order_notional = fresh RUB money − blocked; max_exposure = fresh RUB equity; max_outstanding_orders = positive non-currency position count; max_daily_loss = 5% immutable current-day baseline equity. Исторические суммы не копировать.
Текущий код: src/sentinel_contracts/trading_admission.py AdmissionLimits canonical ACCOUNT_GROSS; update_limits в src/moex_sentinel/services/trading_admission.py. По решению пользователя IN_WORK достаточно, отдельные GOLD/ARM/reconcile ручные допуски не возвращать. Старые helpers с CANARY/ARM не запускать.

## Scope, ограничения и DoD
- SSH/read-only supported API разрешены как продолжение поручения проверить/возобновить PROD. Подготовить и независимо проверить точную штатную запись caps по прежней формуле, до исполнения root сверяет свежие gates и артефакт.
- Не выбирать новые финансовые значения/формулы, не выполнять test post/cancel у брокера, Resume пользовательского HOLD, ручное resolved для UNKNOWN, owner transfer или повторный deploy без выявленной необходимости.
- Сохранить TEST, private ENV, историю, volumes и dirty workspace. Не делать SQL/SQLite reads/checks на PROD. Нет Git branches/stage/commit/push.
- Приёмка: свежие supported API подтверждают release/runtime/owner/lease, baseline, limits и конкретные denial reasons; при допустимости ранее утверждённой настройки — caps записаны CAS и подтверждены GET. Проверка обычного алгоритма наблюдением, без принудительной сделки. Если внешняя причина остаётся — точный blocker и следующий шаг вместо заявления «торговля работает».
- Кросс-ревью независимым агентом helper/финального результата обязательно. Тесты только синтетические/отдельная тестовая БД.

## Пакеты и статусы
1. Сверка свежего runtime root + независимая локальная проверка действующего API/формулы — COMPLETE (12:47:51UTC snapshot; architect и reviewer read-only current code).
2. Ограниченный operational caps helper отдельным author — COMPLETE (trading_resume_ops;29root tests PASS).
3. Независимое pre-execution review и одна штатная CAS запись caps — COMPLETE (15→16;13:12:44UTC).
4. Повторный GET, независимый config review и контекст — COMPLETE (13:14:21UTC; C0/I0/M0).

Config scope 1–4 COMPLETE:29roothelpertests/styles, independentexact/preapply/finalreceiptsreview C0/I0/M0; caps CAS15→16/effective true подтверждены13:14:21UTC. Общаяtradingзадачанеcomplete: SIBNINWORK приNO_ACTIVE_COMMAND/unresolved1. Следующаявеха сверенаrootдоdecomposition: [command-sync-milestone.md](command-sync-milestone.md). Дальше диагностическийsupportedruntimeprojection→точныйrepair, никакогоblindINWORKbootstrap/clearreserve.

## Актуальные шаги после диагностического rollout
1. Command-sync source/OPS и независимое review — COMPLETE; root1185 source tests, continuation62 OPS tests PASS.
2. Diagnostic dd5 upload/build/imports и controlled rollout — COMPLETE14:40:53UTC. Единственный owner transfer→0767/gen7/rev17; caps сохранены. Повторять historical rollout/continuation нельзя.
3. Fresh command_sync + normal Worker logs — COMPLETE. SIBN HOLD3/no active/no outbox/not grouped; GOLD user HOLD6/UNCERTAIN/grouped. GET submit-attempts исходногоowner/gen5 стабильноHTTP422; это доказанный recovery blocker.
4. A Core query parsing + B persisted hold_reason read projection — SOURCE COMPLETE. Root259 regression+12finalroutePASS, independent combinedGO C0/I0/M0. Пландоdecomposition: [submit-attempt-http-milestone.md](submit-attempt-http-milestone.md).
5. Core-only immutable package/offlinebuild — COMPLETE; source4hashfreeze, full350manifestonly4deltas, candidatecore-3a8fc2ddcb55 imagea905b384. Config equality/import smoke PASS, independent exact offlineGO0. Активный Core144ee пока не переключён.
6. Core-only deployment helper — SOURCE COMPLETE31rootPASS/independentGO0; ONE live attempt FAILED CORE_ROLLBACK_UNHEALTHY. Actual Core startup12.68s не уместился в прежний budget; old144ee healthy/oldmarker restored, owner7 lease expired. Повторять этот launch нельзя.
7. Новый stopped-owner recovery — IN_PROGRESS, scope/DoD до делегирования в expired-owner-recovery-milestone.md: cleanstop141160, installcheckedCorea905 с90shealth, one sameWorkerimage/storage newprocess/PENDING, ONE CAS7→8/caps preserved. Corrected proposal/rootACK границ записаны; source/runtime recovery ещё не выполнены.
8. Fresh normal recovery GET200, truthful outcome UNCERTAIN, supported SIBN hold_reason — PENDING. Затем адресное действие с отдельной свежей сверкой scope/DoD; GOLD user HOLD не снимать, UNKNOWN/reserve не clear/repost. Не объявлять торговлю по effectiveflag/health.

## Проверяемые ограничения
- Последний fresh runtime15:32:42UTC послеfailedattempt: owner7/LEASE_EXPIRED/effectivefalse, rev22 тогда и растёт отrenew409; caps5001.1/5051.94/1/252.5970RUBACCOUNT_GROSS сохранены. OldCore144ee healthy, Worker/прочиеcontainers/HOLD неизменны; marker15:37:13 restoredexact. База18Oct5051.94 принятаавтоматически. recovery_only provenance не gate.
- SSH доступен через отдельный BindInterface=en0/ControlMaster; VPN/routes глобально не менялись. Ранние banner/connectivity сбои исторические, не current blocker.
- Старый prepare alias dd5-core указывает на23f (parentbeforedd5), не наcurrent144ee; оставлен без изменений. Новый uniquealias core-recovery-144ee6ca3d43 точно144ee.
- Нет supported Core ledger/events/orders GET; operator automation summary и broker /details не выдавать за полный ledger proof. Дополнительных неподтверждённых admission gates не добавлять.
- Все тесты изолированные; noPRODSQL/SQLite reads/checks. PrivateENV/TEST/volumes/financialhistory/userdirtychanges сохранить. Gitbranches/stage/commit/push запрещены.
- Полная история прежних ошибок/сетевого блока/rollout — develop/WORK_LOG.md; их не повторять как текущий статус.
