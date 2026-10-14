# Процент квоты в карточке позиции — 10.10.2026

## Свежая сверка и утверждённый scope
Пользователь заменил исходные два параметра одним: максимальный процент доступной квоты. Ответами 10.10 подтверждены свободные деньги / число открытых позиций и применение к BUY, AVERAGING и SELL_PART; защитный стоп и полный SELL_ALL без ограничения. Пример: 5000 RUB / 1 позиция × 10% / 50 RUB за биржевой лот = 10 лотов. Ответ пользователя разрешает реализацию; повторное согласование не требуется.
Fresh source: AGENT_BRIEF.md:102–125, queue docs/phase-1-implementation-plan.md, Worker config/strategies/decision_context/streaming_position_decision/runtime_decision_planner, BrokerSdkSession.get_positions/get_free_cash, Core automation repositories/HTTP/usecases, frontend PositionDetailsView. Сейчас buy_order_lots общий ENV, биржевой lot_size неизменяемый, API и UI настроек нет, активный IN_WORK читает локальную SQLite. create_all не меняет старую SQLite. SDK portfolio содержит currency: их не считать открытыми торговыми позициями. Core close только останавливает автомат; не добавлять ликвидацию по кнопке.
Старое решение 0.7 без индивидуальных ресурсов superseded только новым процентом. Остальные adaptive параметры, risk caps, контракт одна обработка/одна заявка и immutable intents сохраняются.

## Контракт и минимальная реализация
- Nullable Decimal order_quota_percent, 0 < x <= 100. NULL сохраняет прежний buy_order_lots/partial-sell; не устанавливать всем 10% автоматически.
- Доступные деньги уже учитывают блокировки и pending BUY reservations. Квота = свободные деньги счёта / max(1, число положительных noncurrency broker positions этого счёта). Пустой портфель допускает первую покупку через делитель 1.
- Максимальный номинал = квота × процент / 100; BUY использует ask, SELL_PART bid, стоимость биржевого лота = цена × catalog lot_size. Округление вниз; ноль даёт WAIT, не принудительный один лот. SELL_PART также ограничен прежней долей/LIFO/остатком. SELL_ALL и защитные сигналы прежние.
- Процент ограничивает номинал, комиссия отдельно входит в existing cash affordability. Пример 500/50 = 10 при достаточном свободном остатке сохраняется.
- Core хранит поле, выводит в публичном DTO и Worker command/status, редактирует с expected_revision + expected_settings_revision и environment scope. Отдельный settings_revision >= 0 изменяется только настройками; execution revision/sequence сохраняются, чтобы pending financial facts оставались принимаемыми. CLOSED не редактируется; менять настройки можно до TRADE. Настройка действует на будущие решения.
- Worker кэширует процент и settings_revision и обновляет уже активные команды из Core, не меняя уже созданные intents/outbox. При недоступности Core не отправлять новые решения на устаревшей конфигурации.
- UI: одно подписанное поле процента, сохранить/сбросить к общему поведению, формула и исключение полного закрытия. Polling не затирает ввод; route/request races и revision conflict явно обрабатываются. Нет отдельного live-preview API и фронтового расчёта денег.
- Core 0005 + проверенный downgrade; Worker additive existing SQLite upgrade и явный rollback. Откат явно отказывает при потере сохранённых параметров/небезопасных условиях. Тесты на отдельных DB, без mandatory backup/restore/integrity рабочего окружения.

## Пакеты, зависимости и приёмка
Root оркестратор, владеет планом/CURRENT/WORK_LOG/очередью/инвариантами; код исполнителей по .agents senior profiles (gpt-6-luna/high). До записи каждый senior возвращает точные файлы и декомпозицию, root ACK. Один writer общих sentinel_contracts — Core; имя поля и edit HTTP payload сначала фиксируются для Worker/UI.
1. Core/API/контракт/0005 и связанные тесты — отдельный senior.
2. Worker portfolio-count/cash sizing/cache/active refresh/SQLite upgrade+rollback и тесты — отдельный senior.
3. Frontend API/карточка/поведенческие tests — отдельный senior.
4. Независимый reviewer gpt-6.1-sol/high (xhigh при необходимости финансовой интеграции), не автор кода; проверяет целый поток и миграции, C/I устраняются и перепроверяются.
DoD: пример 10 лотов; две automation изолированы; noncurrency/account-scoped count; денежные/целолотные границы; ENTRY/AVERAGING/SELL_PART cap и SELL_ALL exemption; legacy NULL; активное обновление и restart; intents неизменны; CAS/scope/auth/CSRF; миграции old→new→safe rollback; UI dirty/race/conflict; достаточная backend/frontend регрессия и clean changed-file lint/build.

## Свежая сверка общей регрессии 10.10, 13:07 МСК
Root actual-native full suite: 2263 PASS, 3 FAIL, 2 SKIP (pg_dump отсутствует, историческая restore rehearsal), 266.14s. Источник: develop/reports/position-quota-20261010/root-backend-full.txt, root-verification.json. Продуктовые файлы совпадают с freeze, whole-code nonauthor review C0/I0/M0; окончательная приёмка ещё не завершена.
Недостающее: старый PostgreSQL admission migration test создаёт automation через текущую ORM после downgrade до 0003, где новых quota columns нет; два evaluator tests используют SpyPlanner со старым контрактом plan. Scope коррекции только соответствующие test setup/contract assertions; не ослаблять проверки сохранности финансового графа и отката, не менять продуктовый API ради устаревшего spy. Core author владеет tests/integration/postgresql/test_trading_admission_migration.py; Worker author — tests/trading_automaton/services/test_position_decision_evaluator.py. DoD: три упавших сценария + связанные migration/evaluator/planner тесты PASS на отдельном PG, lint, повторный независимый review, затем итоговая регрессия. Восстановление из резервной копии не является требованием этой функции; два SKIP не считать проверенным restore.

## Статус и ограничения
- [done] Fresh read-only map и обязательные уточнения пользователя.
- [done] Senior decomposition / root ACK, RED→GREEN реализация Core/Worker/UI.
- [done] Root integration и независимый whole-code review; существенные замечания исправлены и перепроверены.
- [done] Общая регрессия после test-only corrections: 2266 PASS/2 SKIP/232 warnings, 293.57s; root current PG migration2PASS; frontend215/type+build; Ruff/Black/diff PASS.
- [done] Финальная независимая приёмка source/integration/tests/migrations/receipts: rollout_ops_review C0/I0/M0; все C/I/M исправлены и перепроверены, DONE LOCAL.
Нет новых SSH/deploy/ARM/SDK orders, веток/stage/commit/push. Dirty пользовательские/P3 изменения сохранять. PROD прошлый снимок PARTIAL_AWAITING_FRESH_MARKET_DATA не является текущей runtime проверкой.
