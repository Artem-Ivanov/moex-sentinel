# Наблюдения

19.10 показал HOLD/UNCERTAIN у SIBN и GOLD; текущий runtime ещё не прочитан. Skill запрещает подтверждать торговлю только по HTTP200/heartbeat. Старый collector ограничен двумя тикерами; до использования сверить полный список автоматов.

Архитектор: supported API не раскрывает исходные intent/request/brokerID и времена submit/ACK; это пробел наблюдаемости. Request-only recovery закреплён тестом, отсутствие exchangeID fallback само по себе не дефект. Независимый reviewer: старый collector ограничен2тикерами, только Composeidentity, без health/summary/sessions/versions; автор адаптирует в новом audit каталоге.

Fresh 20Oct17:30:21UTC: actual PROD/TRADE/STRATEGYtrue, Core/Analytics/frontendhealthy/Workerrunning/restart0. Admission effectiveTRUE, ownergen8/rev513/lease17:30:48, bothHOLD9+activeUNCERTAIN/terminalfalse; outbox0failed0, iterations175615. Two recovery/SDK modules actualSHA совпали сcurrentlocal. Public indexHTTP200/0.354s/titleMOEXSentinel; curl trust override sslverify20, не проверка довереннойTLSцепочки. Services/resources17:31:48–50UTC: sharedPGhealthy, portfolio snapshotworkerup; прежнийTESTremoteCore/Worker/Analytics/frontendstopped10days, не свежая авария; hostdisk31%, availableRAM900MiB, noProdOOM. ПолноеbrowserUIне открывали.

ПодтверждённеполныйAPIoperations:serviceчитаетоднуpagecursorNoneиотбрасываетnext_cursor, фильтруетпотом;401<500неполнота. НетexistingGETдат. Существующийsnapshotcollectorумеетpagedinterval—reuseприразработке. ANALYTICS_UNAVAILABLE2warningнеравноcontainerdown/HOLDcause;надоуточнить.
