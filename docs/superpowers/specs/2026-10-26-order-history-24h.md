# Возраст записей и восстановление торговли — 26.10.2026

Запрос владельца: незавершённые заявки старше24часов считать failed, записи старше24часов — историческими; реализовать командой, проверить и выкатить. Решение заменяет прежний запрет возрастной классификации неизвестных заявок. Возраст не устанавливает фактический брокерский исход.

## R1. Заявка и исполнение
Возраст вычисляется по долговечному created_at заявки: строго now > created_at +24часа, timezone-aware UTC; ровно24часа не просрочены. Обновление updated_at, перезапуск и попытка чтения брокера не продлевают срок.

Незавершённая заявка получает административный FAILED и private strategy_snapshot._age_policy={version:1,expired_at:UTC-ms-ISO}. При неизвестном брокерском исходе terminal_at остаётся NULL. В существующем OrderStateChanged событии передаётся фактический from_state, FAILED и safe_reason=ORDER_AGE_LIMIT_EXCEEDED. Существующие суммы, исполнения, externalID и created_at неизменны. Это не CONFIRMED_NOT_SENT.

Такой intent не участвует в новом dispatch. Старый idempotency key навсегда сохраняется и повторно не отправляется. Отдельная ограниченная recovery-выборка продолжает exact request reconciliation без POST. Найденный активный ордер сначала отменяется штатным путём с подтверждением terminal. Поздний подтверждённый fill обрабатывается обычной финансовой цепочкой ровно один раз; окончательно завершённые исполнения не переписываются.

## R2. Резерв и технический HOLD
Core проверяет возраст по собственной сохранённой заявке, scope/origin, фактический from_state и неизменность финансовых полей. Событие лишь фиксирует candidate _age_policy в reservation payload, без немедленного release.

Existing automatic account reconciliation вызывается из periodic daily preparation при наличии age candidate или устранимого технического HOLD, в том числе при HOLD. Резерв освобождается одной транзакцией только по свежему полному снимку: актуальные owner/generation/lease/config, полные позиции и ordinary/stop orders, отсутствие активной старой заявки и неизвестных/чужих активных поручений, совпадение broker positions с Core lot ledger. Неизвестный snapshot не обходится; исключение request-level REQUEST_NOT_FOUND касается только точного проверенного age candidate. Неподдержанный lookup или другие неизвестные причины остаются блокировкой.

Settlement: SETTLED, нулевой остаточный резерв, reason AGE_POLICY, broker_outcome_unknown=true, новый согласованный account anchor. Baseline, daily loss, consumed keys, финансовая история сохраняются. Позиционный сдвиг до обработки позднего исполнения блокирует новые поручения существующими guards.

Восстанавливается только HOLD с проверенным источником последнего технического перехода, suspended_from_state=IN_WORK, устранённой причиной и CAS ревизии. Core устанавливает только resume_requested=true без смены состояния/revision. Worker после атомарной проверки пустого outbox и точного command revision/sequence предлагает типизированный HOLD→IN_QUEUE; Core проверяет persisted request и происхождение HOLD, принимает факт и очищает флаг. Далее работает штатный IN_QUEUE→IN_WORK. Прямой перевод Core в IN_WORK/IN_QUEUE до доставки Worker facts запрещён. При отмене пользователем неполученный recovery proposal отделяется от неизменённых финансовых фактов: по авторитетной границе Core они сохраняются и повторно доставляются с согласованными sequence/revision. Неизвестная граница, bootstrap или чужие statefacts не дают права на такую коррекцию. Пользовательский HOLD сохраняется. Повторный явный пользовательский HOLD поверх системного фиксирует пользовательскую причину и новую revision. Already-terminal intent без резерва также не должен навечно оставлять системный HOLD. Общий HOLD→IN_WORK не разрешается.

## R3. Исторические проекции
Для временной записи historical=true только когда captured_at > persisted created_at/occurred_at +24часа. У каждой проекции есть время наблюдения; граница строгая. Позиции, открытые лоты и master configuration не прекращают существование по возрасту. Завершённый FILLED остаётся FILLED с отдельным historical=true. Никакие строки, outbox, ACK, ключи или финансовые факты по возрасту не удаляются и не теряются из доставки.

Признак добавляется в execution diagnostics (orders/reservations/events/executions/accepted envelopes/Worker intents), историю решений и операций. Shared time helper задаёт одну политику сравнения. Новых экранов и настроек срока не требуется:24часа — прямое решение владельца.

## R4. Проверка и rollout
До выкладки нужны isolated RED/GREEN тесты:24часа/24часа+1ms, invalid/future timestamps, replay/restart, malformed markers, отсутствие повторногоPOST, partial/late fill exactly-once, активный ордер, неполныйsnapshot/ledger mismatch, technical vs user HOLD, повторныйuserHOLD. Независимое cross-review без неисправленных Critical/Important.

Root — единственный live executor. Выкладываются только проверенные source patches поверх аттестованных immutable Core/Worker images, сохраняются ENV/limits/TEST/volumes. Нет миграции по принятому проекту. Controlled worker handoff использует доказанный stop прежнего процесса и свежий candidate, не одно истечение lease. Normal postdeploy acceptance:health/ping/UI. Отдельная incident-проверка наблюдает свежие решения, заявки и доставку; работа контейнера не доказывает торговлю.

Свежий исходный срез26Oct14:06–08UTC: оба автомата technicalHOLD9, GOLDUNCERTAIN16Oct/reserve18, SIBNCANCELLED/noreserve. Послеrestart23Oct02:14UTC WorkerPENDING, owner10leaseистёк23Oct02:11UTC, baseline22Oct. Это дополнительные runtime blockers, устраняемые штатной передачей ownership и автоматическим daily preparation, а не возрастной перекраской старых строк.

Локальная реализация/evidence: develop/.planning/2026-10-26-order-history-24h/, develop/reports/trading-recovery-24h-20261026/. Авторы: recovery24_backend, recovery24_core, recovery24_history; независимый архитектор/ревьюер recovery24_architect. Статус implementation/review/deploy подтверждается очередью и runtime report, этот документ задаёт контракт.
