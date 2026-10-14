# TEST purge и четыре улучшения БД

Scope/DoD: docs/superpowers/plans/2026-10-11-database-improvements-and-test-purge.md.
Пользователь разрешил все TEST данные удалить и четыре направления реализовать; PROD, оригинальные volumes и пользовательские изменения сохраняются. Без branch/stage/commit/push, deployment/ARM, mandatory backup/restore/integrity.

- [x] Проверить актуальный scope/контракты/зависимости и записать решения до делегирования.
- [x] Шесть последовательных source-задач с отдельными авторами и независимым ревью.
- [x] Upgrade/downgrade на отдельных PostgreSQL/SQLite; узкий индекс по измерению repository.
- [x] Независимое финальное source ревью C0/I0/M0; 2578 тестов и65Pythonlint PASS.
- [x] Actual TEST purge completedexit0/fresh24TESTzero; all4knownjobprocesses/backendabsent.
- [x] Fresh TEST residual0/originalerrors0/processabsence; ownedtemporaryindexdrop+absence.
- [x] Fresh health/ping/basicHTML/API/resources/fences/PVs; independentopsApprovedC0I0M0/I1CLOSED.
- [x] Итоговые docs/queue/context +SDD39filesarchiveSHAverified; independentfinaldocsreview ApprovedC0I0M0.

Source CODE_DONE / NOT_DEPLOYED, livehead0005/sourcehead0007. Подробные proof и ограничения: docs/audits/database-improvements-20261011.md, develop/reports/database-improvements-20261011/.

## Финальная приёмка
Status: complete
Source, actualTESTpurge, source/ops/docs cross-review и локальный архив завершены. CODE_DONE/NOT_DEPLOYED сохраняется для новых исходников; торговая приёмка отдельная.
