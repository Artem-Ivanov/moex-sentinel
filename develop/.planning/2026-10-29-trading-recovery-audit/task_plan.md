# Проверка торговли и задача восстановления — 29.10.2026

## Актуализация до делегирования
Запрос: применить skills, проверить торговлю, описать причины задачей; оценить отмену зависшей заявки либо завершение отсутствующей у брокера и возобновление торговли.
Сверены: AGENTS.md, AGENT_BRIEF.md (границы Core/Worker, permanent failure, правило24h), docs/phase-1-implementation-plan.md:1–49, develop/CURRENT.md, текущие runtime/analytics_broker.py и services/order_lifecycle.py. HEAD/исходный status: master, docs/phase-1-implementation-plan.md уже изменён предыдущим аудитом; develop/.planning/2026-10-28-trading-audit/ untracked. Чужое сохранить.
Выполненное ранее: реализация24h и локальные F1/F2; прошлый аудит28Oct. Устаревшее: любой runtime вывод28Oct не подтверждает29Oct. Недостающее: свежие runtime/order/reserve сведения и точный permanent RPC error.
Scope: свежие bounded read-only SSH/API/allowlist Docker evidence; офлайн анализ кода; локальные report/tasks, очередь и контекст. Не выполнять автоматическую отмену/Resume/SQL writes/restart/deploy. Вопрос изменения runtime решается после конкретного диагноза; удаление финансовой истории не входит в безопасное восстановление.
DoD: свежие интервалы/версии/ограничения; раздельно BUY139 и SELL94; причины доказанные отдельно от гипотез; исполнимая задача безопасного восстановления, отсутствие повторного submit, сохранение late fills/reservations/history; независимое итоговое review C0/I0.

## Этапы
- [complete] 1. Проверить существующие collectors и собрать свежие read-only evidence.
- [complete] 2. Независимый read-only анализ callgraph восстановления/ограничений отмены.
- [complete] 3. Оформить report/tasks, актуализировать очередь и контекст.
- [complete] 4. Независимое кросс-ревью итоговой интеграции; финальная проверка.

## Владение и зависимости
Root: план/контекст/сбор runtime/интеграция очереди. Architect: только офлайн код и рекомендации. Reviewer: безопасность collectors до запуска, затем итоговый аудит. Report writer: report/tasks после root evidence. Все live действия только root, только чтение. Новых веток/stage/commit/push нет.

## Решение после свежего сбора
29Oct15:49–15:51UTC: runtime по-прежнемуBLOCKED, исходнаяRPCошибкаUNKNOWN; BUY139UNCERTAIN55h29, separateSELL94IN_FLIGHT. Scope оформления: объединённая P1 задача диагностики и безопасного восстановления, раздельные recovery ветки, никаких runtime mutations в этом аудите. Существующие age/cancel/account-settlement механизмы использовать после сверки exact evidence, не разрабатывать альтернативный DELETE. Report/tasks владелец audit_writer, очередь/контекст root, reviewer audit_reviewer. ДоD дополнен: stale26Octreconciliation/outstanding0 не выдавать за freshbrokerorders, D1UNKNOWN/limits явные.

## Завершение
Свежий read-only аудит и одна P1задача оформлены; queue/CURRENT обновлены. Независимое итоговое review audit_reviewer GO C0/I0/M0 после исправлений ageFAILED/settlement, fail-closed wording и точности marker. Root6pytest/application37selfchecksPASS. Торговля не восстановлена и не выдаётся за восстановленную; конкретный следующий шаг в tasks.md.
