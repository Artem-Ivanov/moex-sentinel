# Список задач для запуска PROD — 14.10.2026

## Актуальность root до декомпозиции
Пользователь просит сформулировать список задач, не выполнять исправления/деплой/торговые операции. CURRENT/gitstatus/актуальные source иqueue сверены. Источники: диагностика13.10 trading-blockers иhold-resume (свежиена13.10API/SDK/bundle+independentACK), P3spec, сегодняшниелокальныеruntime.ts/admission_risk/service/repo. Сегодня14.10runtime заново не проверялся; beforeactivationfreshcheck обязателен.
Выполнено: P3backend/Workerfailclosed/lease/permanentkeys/operatorAPI иETF→FUND, TESTretirement. Открыто: UI TEST-only ImportantI1, marklasttrade≤1s versusfreshbook, manualbaseline/no dailyworkflow, current13.10STOP_REQUIRED/recovery_only/nocaps/recon/ARM/userHOLD. Source0006/7 DBimprovements отдельно, не объявлять условием торговли без dependencies.
Scope/DoD: конкретныйприоритетныйbacklog с критериямиприёмки/зависимостями/границамиCodevsOps; различать первыйконтролируемыйзапуск и дальнейшуюавтономнуюработу; независимыйplanreview; сохранённыйtrackedplan/queue/CURRENT/WORKLOG. Новыеpolicychoices (marksource/freshness, dailyprep/ARMapproval) являются задачамипроектирования, не самовольноутверждённымкодом. No SSH/controlwrites/ARM/Resume/orders/deploy/Gitmutations. Не обойтиuserHOLD/resetloss/owner/invariants; tests onlyisolatedDB, ordinarydeployhealthpingUI/no mandatoryfullbackuprestore.
Rootпишетплан/контекст; reviewerread-onlyоцениваетscope/safety/completeness/minimality без реализации/SSH/тестов. Authorнеединственныйрецензент.

- [x] Tasklistdraft с knownfiles/callers/DoD.
- [x] Independentreview/refinement.
- [x] Записатьqueue/context и передать userсписок.

## Review I1: порядок P4/P5 актуализирован
Независимыйreviewer выявил Important: originalproduction-api198–224 требуетstopcommand/alertrecipient/UNKNOWNprocedure иremainingread-onlyfull-day/closed-marketP4доP5canary. Rootсверилprimaryplan+observabilityO2; прежняяARMauthorization не доказываетзакрытиеacceptance. PT4план включаетminimalnotifications/recipient/criticalcases; PT7разделён freshmanualprep/remainingP4→acceptedP3P4→explicitARMResume→P5firstrealcycle→laterliveday/PT3nextday. No extrafullbackuprestoremandatorydeployment; backups отдельныйрегламент. Авторизациянаполныйсписокнеавторизуетканалуведомлений/autoARM; no runtimeexecution. Повторноеreview послеpatch обязательно.

Finalindependentprod_admission_core_review C0I0M0 ACK updatedSHA972696c..., planI1closed. Implementation/tasksnotstarted; useraskedtasklistonly.

## Реализация: свежая сверка root до декомпозиции — 14.10
Пользователь явно поручил делегировать и реализовать доработки, затем сам merge вmaster; productionrollout/checks только ПОСЛЕmerge. ДоступенпроверенныйPT1–7план; текущиеисточники/CURRENT/queue/gitstatus сверены, checkoutmasterссущественнымpreexistingdirty. Реализуем локальныеPT1–PT5 и конкретныеrunbook/preflight/testматериалыPT6–7; actualdeploy/P4/P5/SDKwrites не выполняютсявэтойфазе. NoSSH/networkVPS/ARM/Resume/новыхbranches/stage/commit/push. СохраняемпользовательскийHOLD иdirtysource, existingvolumes. ВсепроверкиБД толькоdedicatedlocalPG55433.
PT1потокспроектирован: frontendTRADEобаKNOWNcontours, falseREAD_ONLY/unknown/loading/allcallers tests. PT2правкиfreshmark требуютSDKsource/pricepolicyproposal→ROOTACK и independentdesignreview; preservepermit1s/RTT/finalclocklocks/permanentkeys. PT3Coreownsdaybaseline, automaticpreparationбезimplicitARM/Resume, immutablebaseline+idempotentday, scope/filemapдорабатываетсяархитектором. PT4alertschannelaskedoptionalasync; покавыбранаконсервативнаяготоваякконфигурацииHTTPSwebhookреализациябездоставкипользователюдоmerge/deploy; ответможетизменитьвыбор. Старыеformulaчислаприниматьтолькопофрешданным, no inventedcaps. Alertrecipient/stop/UNKNOWN/P4 remainfutureoperationalacceptance.
Rootоркестратор/доки/context/интеграциятестов. SeniorFE/backend6-luna/high по .agents; архитектор/reviewer6.1-sol/high(xhighfinancialallowed). Каждыйseniorточныйproposal/fileowners→ROOTACK beforewrites. Readonlyreviewerнеавтор; C/Iисправитьи повторноreviewдоReadyForMerge. Допускаетсяпараллельнаяработатолькоразныхfiles. Dirtybaselineфиксируетсядоproductwrites.
Ruling: Реализуемвсуществующемmastercheckoutбезсозданияbranch/worktreebranch — прямойзапроспользователяредактироватьworkspaceиправилоNoNewBranchesприоритетнееskillизоляции; ценаошибки — смешениесpreexistingdirty, поэтомуbaselineкопия/точныевладельцы/fileSHA обязательны.
Ruling: МатериалыSDDхранимвdevelop/.planning/.../implementation/, сохраняемledgerдомерджапользователя — userAGENTSauxlocation/persistentcontextвышеskill.superpowers/workspacecleanup; цена — дополнительныелокальныеартефакты, нетпотериистории.

## Fresh dependency recheck PT3/PT4 container config14.10
Rootread compose.yml:backend53–75 usesexplicitenvironment allowlist, noenv_file; remote/production overlaysinheritbase. MerelyaddingSettings/envexampleswouldleavefeaturesdisabled/Telegramunconfiguredinsidecontainer. Neededboundedscopeextension2files compose.yml/tests/test_compose_config.py, singleCorewriter; backend-onlyrefs/noAnalyticsWorkercollectorcredentials, preserveexistingnetworks/volumes/userdirty. ROOTACKafterexactseniorproposal. Runtime/deployunchanged.

## Freshfullsuite dependencycheck — APIbuilders14.10
Nativefullsuitefailedatapi(test_automation_contract) onlifespanapp243 newrequiredApplicationUsecases.daily_admission_preparation absentinstaleSimpleNamespace builders148/323. IsolatedAPI-x proof20PASS/1FAIL, actualproductioncompositioncreatesfield; reviewerconfirmedtestdoublecontractregression notruntimefallthrough. Rootstoppedinvalidfullrunexit130; boundednecessarytestscopeextension tests/api/test_automation_contract.py toCoreauthorROOTACK realcomposition+model_copy fixtures, preserveallAPIassertions/userdirty; no productiongetattrweaken. Fulltests/APIthenfullnativeagain. No operationalwork.

FreshAPIbuilderrg14.10confirmedstaleSimpleNamespaceAppbuildersin7additionalfiles lifecycle/concurrency/market/catalog/strategy/fact-ingress/summary. RootACKCoreauthorfixture-onlyexact8filesinclfirstautomationcontract; no viewdependency-onlystubchanges, no sourcefallback/financialguardweakening. FullAPIvalidationbeforefullnative rerun.

## Закрытие авторизованного localimplementation scope14.10
PT1–PT5LOCALDONE: code/tests/crossreview/metadata complete; no open C/I, knownM1duplicatevalidatornotblocking. Rootcurrentcode/queue/agreements/DoD recheckedwithfinal45stableSHA+freshnativeproof, userdirtypreserved. Authoritativecompletionreport develop/reports/prod-trading-start-implementation-20261014/report.md. UsermergebeforePT6, PT7liveacceptance notclaimed/operationaldeferred. No new operationalauthorization inferred.
