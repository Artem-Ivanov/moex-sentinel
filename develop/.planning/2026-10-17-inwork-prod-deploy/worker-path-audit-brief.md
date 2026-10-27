# Read-only audit brief — Worker PROD path

## Goal
Identify actual TEST-origin stubs, no-ops, or incomplete behavior selected by Worker PROD composition; separately identify intentional TEST/Sandbox functionality that must remain. Report production completeness gaps across the Worker trading lifecycle.

## Scope
Inspect Worker config/composition/adapters/services/storage/contracts and Analytics client boundary. Trace PROD TRADE from configured Worker settings and Core runtime registration through streaming/market snapshots, decision, limits/admission, durable intent/idempotency, broker submission/cancel/status, restart recovery/UNCERTAIN reconciliation, SQLite/outbox facts, heartbeat/progress/diagnostics. Check deployment/entrypoint defaults only where they affect selection of PROD behavior.

## Constraints
- Read-only: do not edit product code, tests, configs, or docs; do not call VPS/SSH, PROD API, SQL, or SQLite.
- Do not classify code as a stub based only on `TEST`, `return None`, `pass`, `fallback`, or fake/mock naming.
- Keep test doubles in tests and preserve genuine Sandbox behavior.
- No order submission or any runtime mutation.

## Deliverable
Write `worker-path-audit.md` in this task directory. For each finding provide severity (Critical/Important/Minor), file/symbol, concrete PROD execution path, failure scenario and user impact, evidence/tests (including gaps), smallest correction. Separately classify reviewed candidates that are valid/intentional. Include a lifecycle coverage map and unresolved external/API or broker-contract assumptions. No implementation.
