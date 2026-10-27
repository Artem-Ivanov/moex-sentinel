### Task 3: Scoped operations history (D3)

**Owner:** operations_history_senior gpt-6-luna/high, separatefromD1. Finalsinglepagecontract зафиксирован в Spec/D3 final contract; formalreview/rootACK beforeimplementation.
**Files:** src/moex_sentinel/domain/portfolio.py,services/portfolio.py,usecases/portfolio.py,views/portfolio.py,views/schemas/portfolio.py; newtests/api/test_portfolio_operations.py and designatedservice/usecase/adaptercontracttests. Existingcomposition/app.pyareD1writer: use existingview_recent_operations.execute_scoped forwiringwithoutoverlap, orrootACKsequentialhand-off.
**Interfaces:** GET/api/operations/scoped broker/account/from_at/to_at/limit/cursor; legacyGET/api/operations unchanged. Oneunderlyingpage (limit1..500), returnallselectedfilteredrows/no trimmingtail, explicitnext_cursor/sourcecounts/continuation/completeness. Validateownership/environment/returnedaccount, timezoneaware→UTC, datebounds; noarbitrary366days/UTCzerooffsetbusinessrestriction. Non-nullinputcursor/externalerror/filtershortpage neverfullintervalproof. ExactJSONandstatuserrors в Spec/D3 final contract (input_cursor,next_cursor,sourcecounts,exhaustion,coverage).
- [ ] RED sourcepagefiltered/nextcursor, twosuccessiverequests/dedup/order/bounds/crossscope/readonly/HTTPauth+validation; preserveexistingAPI/fixtures.
- [ ] MinimalGREEN, focusedportfolio/API tests andBlack/Ruff. Nofinancialsnapshot/recoverychanges.
- [ ] task-3-report.md results/diff/hash/limitations; independentscopedreviewandfixes.

## Integration and deployment preparation
Rootruns sufficientcombinedbackendregression (includingisolatedPG), checksbaselinepreservation andquery/runtimecompatibility, updatesdocumentation/envonlyifneeded. IndependentfinalreviewmustcloseallC/I. Noautomaticdeployment, noWorkerresumeclear/repost. UserreceivesconcreteimplementationstatusandD2remainingevidencedependency.
