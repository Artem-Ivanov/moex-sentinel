# Worker D1 diagnostics profile — 2026-10-27

Synthetic-only profile. The benchmark creates temporary SQLite files from the
Worker ORM schema and deletes them on exit; it never opens or copies a runtime
database. Run from the repository root:

```sh
PYTHONPATH=src .venv/bin/python develop/benchmarks/20261027_diagnostics_d1/profile.py
```

Environment: Python 3.12.14, SQLite 3.53.1, SQLAlchemy 2.0.51, macOS
26.7.1 x86_64, 16 reported processors. SQL and full-reader latency values are
medians of seven warm runs. The observation, reason, and row count in the table
are from the seventh (last) full-reader run. `before.json` and `after.json`
contain all four SELECT plans, query medians, and full-reader results for 100,
1,000, 10,000, and 50,000 total intents, each distributed over either 2
automations or the 100-automation cap. The same 100 ms reader budget is used in
both runs.

| Automations | Total intents | `_INTENTS` before | `_INTENTS` after | Full read before | Full read after |
|---:|---:|---:|---:|---:|---|
| 2 | 100 | 1.412 ms | 0.806 ms | 5.377 ms OBSERVED | 5.220 ms OBSERVED |
| 2 | 1,000 | 8.621 ms | 3.772 ms | 11.836 ms OBSERVED | 6.301 ms OBSERVED |
| 2 | 10,000 | 91.341 ms | 36.387 ms | 102.190 ms UNKNOWN/BUDGET_EXCEEDED | 32.956 ms OBSERVED |
| 2 | 50,000 | 477.560 ms | 224.628 ms | 101.829 ms UNKNOWN/BUDGET_EXCEEDED | 105.521 ms UNKNOWN/BUDGET_EXCEEDED |
| 100 | 100 | 2.339 ms | 2.133 ms | 12.619 ms OBSERVED | 10.857 ms OBSERVED |
| 100 | 1,000 | 17.016 ms | 12.220 ms | 39.419 ms OBSERVED | 36.950 ms OBSERVED |
| 100 | 10,000 | 89.474 ms | 51.208 ms | 115.688 ms UNKNOWN/BUDGET_EXCEEDED | 98.538 ms UNKNOWN/BUDGET_EXCEEDED |
| 100 | 50,000 | 473.571 ms | 297.524 ms | 100.765 ms UNKNOWN/BUDGET_EXCEEDED | 101.691 ms UNKNOWN/BUDGET_EXCEEDED |

Before, SQLite scanned `broker_intents` through
`ix_broker_intents_automation_id`, then carried the wide intent/JSON projection
and performed the latest-decision correlated lookup before removing rows past
the six-row per-automation boundary. It used temporary B-trees for window and
final ordering. After, a narrow `active_candidates AS MATERIALIZED` CTE
computes priority once and ranks only intent IDs, automation IDs, timestamps,
and the active flag. The final projection and latest-decision lookup run only
for the selected six rows per automation. SQLite still uses the existing
automation and decision indexes; no index or schema changed. Query plans are
preserved verbatim in the JSON files.

This profile demonstrates that `_INTENTS` caused the 10k-intent cutoff in the
two-automation case and that the scoped query rewrite brings that profile back
inside 100 ms. The 50k profiles return `UNKNOWN/BUDGET_EXCEEDED`, preserving
the deadline's fail-closed behavior. At the 100-automation cap with 10k intents,
the median latency is 98.538 ms and the last run returned
`UNKNOWN/BUDGET_EXCEEDED` with zero rows. This profile is unstable at the
100 ms deadline. Synthetic data does not establish the production database
size or runtime latency. No timing threshold was added to CI.

## Outbox interpretation

The synthetic schema's `fact_outbox` check permits only `PENDING` and `FAILED`.
`LocalAutomationRepository.acknowledge_fact_outbox()` deletes rows through the
accepted sequence, so ACKed history is not retained in this table. The current
runtime observation supplied for this profile has 0–3 pending/failed rows; the
synthetic reader profiles have one row per selected automation (2 or 100), a
small bounded shape rather than a large ACK-history model. Across those runs,
`_FACT_COUNTS` took 0.12–0.81 ms and used the existing
`ix_fact_outbox_automation_id` index. The saved row counts, status CHECK, and
delete behavior make an additional status `WHERE` redundant for the allowed
physical rows. No outbox SQL, schema, or index change was made.
