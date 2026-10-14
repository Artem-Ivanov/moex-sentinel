# Свежая проверка готовности PROD к торговле — 10.10.2026

## Актуальность и решение root до делегирования, 20:27 МСК
Запрос: «Есть ли какие-то обновления что бы запустить торговлю на прод окружении?».
Scope: проверить текущие health/runtime/owner/admission/торговую доступность GOLD и определить, требуется ли исправление приложения либо подготовка допуска. Предыдущий релиз квоты завершён; исторические STOP_REQUIRED и CLOSED не являются свежими доказательствами.
Источники: develop/CURRENT.md (релиз schema0005, owner gen3, прежняя авторизация baseline/reconcile/TRADE/ARM и формулы лимитов); AGENT_BRIEF.md:78–90 (границы Core/Worker и permit); docs/phase-1-implementation-plan.md:362–379 (P3 readiness остаётся OPEN); src/moex_sentinel/services/trading_admission.py (текущие gates); reviewed collect_api_readonly.py из предыдущего audit.
Выполнено ранее: PROD TRADE/strategy=true, квота установлена, baseline10.10 принят. Устарело: старый owner gen2/порт8443/старые численные caps. Недостающее: текущий API и read-only SDK risk/market status.
DoD: свежие датированные receipts, конкретные действующие блоки и ближайший разрешённый шаг; независимое ревью вывода; CURRENT/WORK_LOG обновлены. Проверка работоспособности health/basic API, без DB integrity/backup/restore. На этой фазе business GET/read-only SDK; заказов, перезапусков, deploy и SQLDELETE нет. Если все условия допуска готовы, подготовка дальнейшего шага опирается на прежнюю авторизацию, без обхода gates.
Root owns SSH, plan/context, итог. Никаких веток/stage/commit/push. Отдельные helpers/evidence только develop/. Автор helper не единственный рецензент.

## Шаги
1. [done] Текущие защищённые API и risk/market read-only.
2. [done] Проверка текущего кода и необходимости нового релиза независимыми quota_core/cleanup_ops_review: ETF/FUND blocker.
3. [done] Кросс-ревью результата и обновление контекста; C0/I0/M0, trading OPEN.

## Актуализация вехи root до делегирования исправления
Свежий SDK GOLD возвращает ETF. Connected-source review обоих агентов: каталог SDK etfs→FUND (market_data.py:62–70), risk adapter оставляет ETF (admission_risk.py:327); repository:458 требует совпадение с каталогом, service:750 принимает SHARE/FUND. Это будущий DATA_UNKNOWN на submit независимо от сегодняшнего stale/closed. Предыдущая формулировка «новая версия не требуется» отозвана.
Согласованный ранее контракт P3 spec:165 поддерживает SHARE/FUND; пользователь разрешил торговлю именно GOLD. Исправление нормализует SDK ETF→FUND в существующей границе adapter, не добавляет класс инструмента и не ослабляет risk gates. Этот bounded дизайн следует уже согласованной поддержке фонда/GOLD; нового функционального решения/лимита нет.
Scope/достижимый DoD этой фазы: минимальный adapter fix, regression RED→GREEN с реальным SDK ETF и проверка связанного submit path/отказов неподдерживаемых типов; appropriate targeted tests/style, независимый повторный review. Callers collect→reconcile/authorize и repository canonical type contract; baseline instrument=None не затрагивается, config/export/schema не меняются. БД/миграции не нужны. Root отдельно read-only сверяет deployed source hashes. Патч готовится локально; автоматический deploy/ARM на этой фазе не выполняются, рынок CLOSED/stale остаётся отдельным ограничением.
4. [done] quota_core — реализация нормализации и meaningful regression tests, RED→GREEN43.
5. [done] root100tests/Ruff/Black/diff PASS + cleanup_ops_review независимое code/result ревью C0/I0/M0.

Итог фазы: READY_LOCAL_NOTDEPLOYED. Проверка текущего состояния и локальное исправление завершены; production release и real trading acceptance не выполнены, остаются следующими шагами. Единственные production изменения две строки canonicalETF→FUND; независимый reviewer воспроизвёл исходный deployedSHA при их удалении. Никаких миграций, allowlist/gates/config changes, deploy/ARM/orders/Gitactions.
