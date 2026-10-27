# Findings

D1diagnosticsможнобазироватьнаexistingWorkerpushprojection; command_sync_diagnosticsсейчасскрываетexecutionfields. D3contractViewRecentOperationsUsecase→PortfolioService.view_operations(limit)→1brokerpage; adapterужеfrom/to/cursor. D2зависитотmissingruntimecorrelation, request-onlylookupнеобъявлятьbug.

Preflightreview: D1heartbeatextra=forbidтребуетcapabilitymarkerиwireexclusionдляoldCore, immutableprincipal/runtime/broker/accountscope иисключениеoptionalbadprojectionизcontrolheartbeats. Неэкспонироватьexecutionчерезglobalstatusбезscopejoin. D3выбранsingle-source/accountpageчтобыcursorне терялхвостselectedlimitприагрегации; старыйGETUIне ломаем. ИзбыточныеUTC0only/366dayограничениянеприняты,датыnormalizeUTC.

## D1 final review + D2 integration
D2 doc finalintegration первоначально выявила неверную прямую связь Worker execution business ID с envelope ID; root рекомендация исправлена через реальный graph. Docauthor исправил, reviewer audit_review_20261020 повторно принял C0/I0/M0 на runbook53813... /report80e412... (task-2-final-review.md).
D1 author freeze task-1-report.md:97focusedPASS, Ruff/BlackPASS, root sourcehashes совпали. Новые final review packages worker17files/core11files сформированы против исходного dirty baseline. Root fullbackend +explicitisolatedPG вработе backend-final-root.log. Архитектор предварительно подтвердил C1/C2/C3 закрыты; Minor ложный truncated при большеlimit envelopes будет исправлен после полного прогона отдельным regressioncase (непропускать falseincomplete).

Freshdeploymentinputs: status-before.json captured19:22:56UTC, owner8/rev513/effectiveopen, 2HOLD9; runtime-source-before.json19:24:03UTC. Corebase251py==preimplementationbaseline; WorkerunusedCore10differencesexemptbypreservedimmutableparent, runtime-relevantbaseline differences0. source-parity.json. Service-specific18/5patchmaps avoid unreviewed localdirty. ArchitectGO controlled existingowner-stoppedCAS asnecessaryauthorizeddeploy; noextrausergate, noSQL/clear/repost.
