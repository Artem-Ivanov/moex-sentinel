# Проверка изоляции и завершённости — 08.10.2026

## Fresh постановка до декомпозиции
Пользователь просит аудит текущей реализации, уровней изоляции сервисов и завершённости последних доработок. Это read-only проверка кода, конфигурации и локальных тестов; исправления не заказаны.
Источники: AGENTS.md; AGENT_BRIEF.md; README.md; docs/phase-1-implementation-plan.md:361–455; docs/audits/layered-solid-fixes-20261006.md; git status/diff HEAD; compose.yml; tests/test_architecture.py.
Подтверждено: HEAD 8d4805e, master, большой пользовательский staged+unstaged пакет 06.10. Текущий CURRENT посвящён шаблону другого проекта; scope аудита определяется текущим кодом MOEX. Последний кодовый пакет I1–I3/M1–M6 и PostgreSQL follow-up заявлены LOCAL DONE. W3/C5 и broker editor остаются кандидатами, не входят в завершённый пакет.
Устаревшее/недостающее: отчёт 06.10 не подтверждает текущий runtime; свежие тесты и проверка текущих файлов нужны. VPS/deploy неизвестны. TEST/PROD делят одну PostgreSQL/database по согласованному решению; Worker SQLite durable, Analytics market-only.

## Scope и DoD
1. Сопоставить заявленные границы и реальные imports, composition, DTO/HTTP, владение credentials/данными; отдельно process/network/DB/test-prod/access/transaction isolation.
2. Проверить final worktree diff последнего архитектурного пакета против I1–I3/M1–M6, потребителей и meaningful tests; stage/index сохранять.
3. Свежие backend, архитектурные и config проверки; PostgreSQL только на ранее созданном отдельном тестовом project после fresh проверки изоляции. Если недоступен, явно указать непройденную область; рабочую БД не использовать.
4. Отчёт с severity, точным местом, сценарием и минимальным исправлением, матрицей завершённости и границами доказательств. Независимый кросс-review итогового отчёта.
Запреты: branches/worktrees/stage/commit/push, SSH/live broker API/TRADE/deploy/рабочая БД/удаление volumes; не печатать секреты. Source baseline: develop/reports/isolation-completeness-20261008/{git-status-before.txt,index-before.txt,source-before.json}.

## Этапы
- [x] Актуальность постановки и baseline
- [x] Параллельный аудит архитектуры, security/isolation и завершённости
- [x] Свежие тесты в изолированной среде
- [x] Интеграция findings и независимый cross-review
- [x] Финальный отчёт, CURRENT и WORK_LOG

## Владельцы
Root: план, контекст, запуск проверок, интеграция отчёта. Leaf audit_evidence_worker (luna/medium): ONLY develop report/reproduce_findings.py. Leaf audit_report_writer (luna/medium): ONLY docs/audits/service-isolation-completeness-20261008.md; root принимает/интегрирует после независимого ревью. Architect: read-only service boundaries. Security auditor: read-only trust/network/test-prod/data access isolation. Reviewer: read-only completeness I1–I3/M1–M6 и затем cross-review интеграции. Все три профиля gpt-6.1-sol/high; без самостоятельного fan-out или записи.

## Steering пользователя: проверка TEST после релиза
08.10 пользователь сообщает: в TEST нет торговли после релиза, последние операции29.09. Это пользовательское наблюдение, runtime ещёне проверен. Просит записать проверку и объяснить выдачу доступа для редактирования/деплоя; фактическое разрешение SSH/TRADE/deploy пока не предоставлено.
Fresh source: docs/deployment/remote-compose.md:19–33 описывает HISTORICAL04.10 releasef29472f/0.2.0, Workerобаrunning, READ_ONLY/STRATEGY_ENABLEDfalse, TRADEне включён. Это возможное объяснение остановки, не текущаядиагностика. Перед runtimeвехой нужныfresh scopedchecks: exactTESTrelease/images/versions, Worker/profile/heartbeat/control, broker_access_mode/strategy_enabled, automationsHOLD/INWORK/UNCERTAIN/bootstrap, marketAnalyticsTTL/outbox/intents/lastbroker/Core/Workeroperations. Важно отличать отсутствиеновыхexecutions отstaleUI/readmodel. Не автоматическивключатьTRADE/strategyилиотправлятьзаявку.
Доступ: localworkspacewriteесть; network sandbox/auto_reviewпредоставляетконкретныеescallations, неpermissionнаSSHпоAGENTS. НужнаSSHкоманда/alias/путьconfiguredprivatekey(localнеchat) иявныйscopeTESTdiag/fixes/deploy/sandboxTRADE. Managedpolicyнеобходить. NoSSHоперацийсейчас.
Новаяruntimeвехаещёне начата; source/read-onlyplanсвежеподтверждаеттолькодокументированныекандидаты.
