# Read-only audit brief — Core PROD path

## Goal
Identify actual TEST-origin stubs, no-ops, or incomplete behavior that is selected by Core PROD composition; separately identify intentional TEST/Sandbox functionality that must remain. Also report production completeness gaps in Core's part of automated trading.

## Scope
Inspect Core config/composition/API/services/storage and T-Invest Core adapters: market data, portfolio, connection, order/risk/account data, daily admission preparation, trading admission, operator/runtime APIs, readiness notifications, service isolation. Trace constructor/composition paths from settings to runtime behavior. Read the relevant current implementation and tests; historical audit docs are evidence only for their timestamp.

## Constraints
- Read-only: do not edit product code, tests, configs, docs, or call VPS/SSH, PROD API, SQL, or SQLite.
- Do not classify code as a stub based only on `TEST`, `return None`, `pass`, `fallback`, or mock naming.
- Preserve real TEST/Sandbox support; preserve no-send/disabled-by-config behavior as a feature unless a proven product contract says it should send.
- Do not infer current runtime state from historical reports.

## Deliverable
Write `core-path-audit.md` in this task directory. For every finding provide severity (Critical/Important/Minor), file/symbol, concrete execution path showing PROD composition reaches it, failure scenario and practical effect, evidence/tests (including what is not covered), and smallest correction. Separately list reviewed candidates that are valid/intentional. End with an explicit PROD-path coverage map and outstanding unknowns. No implementation.
