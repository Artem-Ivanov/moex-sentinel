# Findings
Вчера: PG8.60GB, automation_events5.951GB (financial envelopes/dedup), trade_audit_events1.874GB, trade_decisions0.737GB; WorkerPROD0.27MiB. Эти размеры неfresh11.10. NULL overhead/Enum benefit пока не доказаны.

## Fresh evidence и синтез — 12:36 МСК
READ ONLY final capture 09:36:12–17 UTC: PG16.15/head0005/24tables/45FK/69indexes/database8611060759B; 14queriesPASS+SQLitemetadataPASS. Evidence develop/reports/database-architecture-20261011/database-live.json. Top3relations99.43%, allindexes1.936GB. Block sample1592envelopes allTEST; 76.5%auditrows/67.7%sampledJSONtext; 1300audit59%emptydata. НеcurrentPRODgrowth. Admission sample364/364OWNER_LEASE_RENEWED; table12.26MB/~39.8krows/defaultheartbeat3s rateconditional. Nativeenums0; exactliveCHECKdefsconfirmNULLholes, corruptedrowsnotclaimed. SQLitePRODmain159744/WAL82432/SHM32768,10tablesnoCHECK, queue/intents/decisions0; metadataonlynottradingacceptance.
Решение: targetedD1CHECK, separateD1bWorker/FSM, D2aordinaryrenewaudit +D2b6→1summary, D3isolatedrun_id/BRINcandidates, conditionalD4permanentreceipt/body. No workingDBtest/DDL/DELETE/deploy/Git. Sources docs/audits/database-architecture-20261011.md + agentnotes.
