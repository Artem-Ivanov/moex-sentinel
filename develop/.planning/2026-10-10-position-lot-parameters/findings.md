# Findings

# Параметры объёма в карточке открытой позиции — 10.10.2026

## Постановка и свежая сверка scope
Пользователь: «вынести в параметры открытой заявки настройки величины лота и количество лотов за обработку». Предварительно речь о редактируемых параметрах автомата открытой позиции; единицы и смысл двух полей ещё не заданы. Async question ожидает ответ: торговая порция в биржевых лотах или RUB, число порций за цикл. До ответа не менять алгоритм/финансовые значения.
Freshкод: lot_size — неизменяемое свойство инструмента из brokercatalog, а buy_order_lots общийENVWorker. Стратегия ENTRY/AVERAGING использует buy_order_lots; cash affordability builder использует тот же параметр. PositionDetailsView показывает позицию и стратегию, формы редактирования объёма пока нет. Existing automation HTTP действия hold/resume/close, command pipeline Core→Worker уже есть.
Источники: AGENT_BRIEF.md:102–125; src/trading_automaton/config.py:21; services/strategies.py:64,184; services/decision_context.py:77; frontend/src/views/PositionDetailsView.vue; frontend/src/api/automations.ts; views/automations.py. Старое решение0.7 не добавлять индивидуальные ресурсы superseded только новым user request на эти параметры; не менять прочие adaptiveпороги/стратегию/финансовыеcaps.


## Уточнение пользователя и решение root
Подтверждены free cash / positive noncurrency position count и BUY/AVERAGING/SELL_PART cap с SELL_ALL exemption. Один editable nullable order_quota_percent. max(1,count) для пустого счёта, комиссия отдельно от номинального лимита. Close automation не продаёт: сохраняем существующий контракт. Незавершённое — реализация, проверки, независимое review.

Свежий root/Worker integration finding: bump общей automation.revision при settings edit конфликтует с immutable expected_revision в PENDING financial outbox и existing rejection переводит в HOLD/FAILED. Root ACK: отдельная settings_revision default0 в Core+Worker/publicDTO/command/status; PATCH {expected_revision,expected_settings_revision,order_quota_percent} проверяет обе, изменяет только settings_revision. Финансовые payload/sequence/revision не переписывать, Core factCAS не ослаблять. Scope/testDoD обновлён и передан всем трем writer.

Independent reviewer rollout_ops_review: frontend I1/M1 закрыты C0/I0/M0. Root frontend full215/type+build PASS, actual isolatedPG0005migration1PASS. Core I2: SQLite batch recreate parent trading_automations with FK ON fails if position_cycle exists; author fixed-scope ACK nativeSQLite ADD/DROP COLUMN + real legacy child regression. PG/CAS/API slice no issues. Worker root finding: postdecision min-only quota does not size BUY from default1→user10; author notified to size effective context before strategy/cashchecks and real strategy tests. Backend/whole integration not accepted until fixes/review.

Worker independent reviewer I3 atomicrollback и I4 actualstatusadapter исправления закрыты в коде; author192focusedPASS/style. I5: Decimal precision28 intermediates can round tinypercent × maxSDKmoney nominalcap вверх до целого лота; concrete6999999999999999999.999999999 ×0.000000003% /1 /1 gives210000000 insteadexact209999999. RootACK exactstdlib Fraction/integerfloor иcounterexample regression. Fullsuitefreeze ждётI5; лишнихфинансовыхизменений/рутинныхfullповторов нет.
