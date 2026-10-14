# Почему нет торговли — свежая диагностика 13.10.2026

## Root: актуальность до декомпозиции
Запрос: проверить, почему торговля не идёт и что блокирует. Источники: текущий запрос; CURRENT11.10; AGENT_BRIEF admission invariants; очередь395–450; фактические Core/Worker файлы и проверенные read-only helpers. Ранее P3 и ETF→FUND исправление развёрнуты, PROD TRADE/strategy разрешены; TEST4 retired/purged11.10. Исторические STOP_REQUIRED/gen3/lease/MARK_PRICE_STALE не доказательство сегодняшнего runtime. Код0006/0007 CODE_DONE/NOT_DEPLOYED, не причина без runtime evidence.
Scope/DoD: сегодняшние датированные API/container/Worker и при необходимости SDK read-only receipts; точные текущие gates и причина; ближайший шаг без обхода защит; независимое кросс-ревью вывода; CURRENT/WORK_LOG/report. Изменять торговый допуск, лимиты, baseline, owner, ARM, отправлять/cancel заявки, перезапускать/deploy/мигрировать/удалять данные в диагностике не требуется. Старое разрешение активации сохраняется для отдельного продолжения при свежих условиях, здесь сначала выяснить причину. DB tests толькоотдельно; финансовуюцелостностьрабочейБД не проверять. БазовыеоперационныеGET/ограниченныйWorkerreadonlysnapshot допустимы.
Root owns SSH/live/evidence/plan/context. Независимый reviewer read-only trace admission/Worker flow и вывод; не liveSSH/не codewrites. No branch/stage/commit/push; preservedirtyuserfiles/allvolumes/secrets. Используются project-context/systematic-debugging/ponytail full.

## Шаги
- [x] Свежие защищённые GET и текущие контейнеры.
- [x] Причина отказа Worker/допуска; при необходимости SDKmarket readonly.
- [x] Независимое review текущих доказательств/вывода.
- [x] Итог и журнал/контекст.

## Уточнение пользователя: почему нет свежей базы дня
Root сверил текущиеcapture_baseline/view/CLI/save_baseline и все sourcecallers: capture вызывается явным operatorPOST/CLI; Workerdailyjob не найден. Это объяснение существующего поведения, без изменениядопуска/данных. DoD: точнаяручнаяточкавхода/immutabledaysemantics/фактlastaccepted10Octизсегодняшнегоreceipt, независимаяпроверка вывода. Текущаяcode/runtimeauthorization не расширяется, новаяbaseline/ARM не запрашивались и не выполняются.

## Новый followup: HOLD → Resume недоступен
Rootсверил CURRENT/sourceHOLDResume/API/FElifecycle; новаяпользовательскаядействиеHOLD делаетпрошлыйIN_WORKreceiptустаревшим. Требуется новыйprotectedGETбезcontrolwrites. Scope: actualstate/runtime +FEdisablepredicate/deployedfrontend/sourcecompatibility, APIresumecontract. DoD: конкретнаяпричинакнопки, отделённаяотaccountadmissiontradegates; независимыйreview. В этойдиагностикене выполнятьHOLD/Resume/ARM/orders/deployсамостоятельно. RootSSH/evidence/context; reviewerread-onlyкод/receipts. Userdirtyсохранить.
