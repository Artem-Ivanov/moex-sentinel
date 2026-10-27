"""Bounded read-only SQLite projection for optional Worker diagnostics."""

import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import Numeric, bindparam, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from sentinel_contracts.broker_order_policy import LEGACY_TERMINAL_STATES
from sentinel_contracts.time import floor_utc_millisecond
from sentinel_contracts.worker_diagnostics import (
    WorkerAutomationExecutionDiagnostics,
    WorkerExecutionDiagnostics,
    WorkerIntentDiagnostics,
)

_READ_BUDGET_SECONDS = 0.1
_CONNECT_TIMEOUT_SECONDS = 0.05
_MAX_AUTOMATIONS = 100
_MAX_INTENTS_PER_AUTOMATION = 5
_TERMINAL = tuple(sorted(status.value for status in LEGACY_TERMINAL_STATES))
_ACTIVE_STATES = (
    "CREATED",
    "DISPATCH_PENDING",
    "SUBMITTING",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "UNCERTAIN",
)
_AUTOMATIONS = text(
    """
    SELECT automation_id, account_id, instrument_id, fact_instrument_id, lot_size
    FROM cached_automations
    WHERE broker_id = :broker_id
    ORDER BY automation_id
    LIMIT 101
    """
)
_UNSCOPED_CACHE = text("SELECT 1 FROM cached_automations WHERE broker_id IS NULL LIMIT 1")
_INTENTS = (
    text(
        """
    WITH active_candidates AS MATERIALIZED (
        SELECT intent.idempotency_key, intent.automation_id, intent.updated_at,
               CASE WHEN (
                   intent.state IN :active_states
                   OR (intent.state = 'FAILED' AND intent.terminal_at IS NULL
                       AND NOT COALESCE((
                           json_type(intent.strategy_snapshot, '$._age_policy.version') = 'integer'
                           AND json_extract(intent.strategy_snapshot, '$._age_policy.version') = 1
                           AND json_type(intent.strategy_snapshot, '$._age_policy.expired_at') = 'text'
                           AND length(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at')) = 29
                           AND json_extract(intent.strategy_snapshot, '$._age_policy.expired_at')
                               LIKE '____-__-__T__:__:__.___+00:00'
                           AND julianday(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at'))
                               IS NOT NULL
                           AND julianday(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at'))
                               - julianday(intent.created_at) > 1
                           AND julianday(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at'))
                               <= julianday(:captured_at)
                       ), 0))
                   OR (:prod_terminal_unfinalized AND intent.state IN :terminal_states
                       AND intent.terminal_at IS NULL
                       AND NOT COALESCE((
                           json_type(intent.strategy_snapshot, '$._age_policy.version') = 'integer'
                           AND json_extract(intent.strategy_snapshot, '$._age_policy.version') = 1
                           AND json_type(intent.strategy_snapshot, '$._age_policy.expired_at') = 'text'
                           AND length(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at')) = 29
                           AND json_extract(intent.strategy_snapshot, '$._age_policy.expired_at')
                               LIKE '____-__-__T__:__:__.___+00:00'
                           AND julianday(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at'))
                               IS NOT NULL
                           AND julianday(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at'))
                               - julianday(intent.created_at) > 1
                           AND julianday(json_extract(intent.strategy_snapshot, '$._age_policy.expired_at'))
                               <= julianday(:captured_at)
                       ), 0))
                   OR (:test_filled_unemitted AND intent.state = 'FILLED'
                       AND intent.execution_facts_emitted_at IS NULL)
               ) THEN 1 ELSE 0 END AS is_active
        FROM broker_intents AS intent
        WHERE intent.automation_id IN :automation_ids
    ), ranked AS (
        SELECT idempotency_key, automation_id, is_active,
               row_number() OVER (
                   PARTITION BY automation_id
                   ORDER BY is_active DESC, updated_at DESC, idempotency_key DESC
               ) AS position
        FROM active_candidates
    )
    SELECT ranked.automation_id, intent.idempotency_key,
           decision.id AS decision_id, decision.process_id,
           intent.state, intent.side,
           intent.broker_order_id, intent.quantity_lots, intent.limit_price,
           intent.requested_amount, intent.executed_lots, intent.executed_amount,
           intent.estimated_commission, intent.executed_commission, intent.executed_price,
           intent.execution_currency, intent.fact_execution_id, intent.execution_facts_emitted_at,
           json_extract(intent.strategy_snapshot, '$.admission_origin.runtime_instance_id')
               AS origin_runtime_instance_id,
           json_extract(intent.strategy_snapshot, '$.admission_origin.generation') AS origin_generation,
           intent.created_at, intent.updated_at, intent.dispatch_started_at,
           intent.broker_responded_at, intent.executed_at, intent.terminal_at,
           ranked.is_active
    FROM ranked
    JOIN broker_intents AS intent
      ON intent.idempotency_key = ranked.idempotency_key
    LEFT JOIN trade_decisions AS decision
      ON decision.id = (
          SELECT candidate.id FROM trade_decisions AS candidate
          WHERE candidate.intent_id = intent.idempotency_key
          ORDER BY candidate.occurred_at DESC, candidate.id DESC LIMIT 1
      )
    WHERE ranked.position <= 6
    ORDER BY ranked.automation_id, ranked.position
    """
    )
    .bindparams(
        bindparam("automation_ids", expanding=True),
        bindparam("active_states", expanding=True),
        bindparam("terminal_states", expanding=True),
    )
    .columns(
        limit_price=Numeric(28, 9),
        requested_amount=Numeric(28, 9),
        executed_amount=Numeric(28, 9),
        estimated_commission=Numeric(28, 9),
        executed_commission=Numeric(28, 9),
        executed_price=Numeric(28, 9),
    )
)
_FACT_COUNTS = text(
    """
    SELECT automation_id,
           sum(CASE WHEN delivery_state = 'PENDING' THEN 1 ELSE 0 END) AS pending_count,
           sum(CASE WHEN delivery_state = 'FAILED' THEN 1 ELSE 0 END) AS failed_count
    FROM fact_outbox
    WHERE automation_id IN :automation_ids
    GROUP BY automation_id
    """
).bindparams(bindparam("automation_ids", expanding=True))


class WorkerExecutionDiagnosticsReader:
    """Read existing Worker state using an independent, bounded SQLite connection."""

    def __init__(self, database_url: str, *, environment: str = "TEST") -> None:
        if environment not in {"TEST", "PROD"}:
            raise ValueError("Invalid Worker diagnostics environment")
        self._database_url = database_url
        self._environment = environment

    def read(self, broker_id: UUID | str | None, captured_at: datetime) -> WorkerExecutionDiagnostics:
        if broker_id is None:
            return WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER")
        try:
            broker_uuid = UUID(str(broker_id))
        except (TypeError, ValueError):
            return WorkerExecutionDiagnostics(reason="UNCONFIGURED_BROKER")

        started = monotonic()
        deadline = started + _READ_BUDGET_SECONDS
        engine = None
        connection = None
        try:
            database_path = self._database_path()
            if database_path is None:
                return WorkerExecutionDiagnostics(reason="UNSUPPORTED_SOURCE", broker_id=broker_uuid)
            if not database_path.is_file():
                return WorkerExecutionDiagnostics(reason="UNSUPPORTED_SOURCE", broker_id=broker_uuid)
            self._check_budget(deadline)
            uri = f"file:{quote(str(database_path), safe='/')}"
            engine = create_engine(
                URL.create("sqlite+pysqlite", database=uri, query={"mode": "ro", "uri": "true"}),
                poolclass=NullPool,
                connect_args={"timeout": min(_CONNECT_TIMEOUT_SECONDS, self._remaining(deadline))},
            )
            connection = engine.connect()
            raw = connection.connection.driver_connection
            raw.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
            self._check_budget(deadline)
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
            self._set_busy_timeout(connection, deadline)
            if connection.execute(_UNSCOPED_CACHE).first() is not None:
                return WorkerExecutionDiagnostics(
                    reason="UNSCOPED_CACHE",
                    broker_id=broker_uuid,
                )
            automation_rows = connection.execute(_AUTOMATIONS, {"broker_id": str(broker_uuid)}).mappings().all()
            self._check_budget(deadline)
            truncated = len(automation_rows) > _MAX_AUTOMATIONS
            automation_rows = automation_rows[:_MAX_AUTOMATIONS]
            if not automation_rows:
                return WorkerExecutionDiagnostics(
                    observation="OBSERVED",
                    reason="OBSERVED",
                    broker_id=broker_uuid,
                    captured_at=floor_utc_millisecond(captured_at),
                )
            if any(
                not row["account_id"]
                or not row["instrument_id"]
                or not row["lot_size"]
                or row["fact_instrument_id"] is None
                for row in automation_rows
            ):
                return WorkerExecutionDiagnostics(reason="UNSCOPED_CACHE", broker_id=broker_uuid)

            automation_ids = [row["automation_id"] for row in automation_rows]
            parameters = {
                "automation_ids": tuple(automation_ids),
                "active_states": _ACTIVE_STATES,
                "terminal_states": _TERMINAL,
                "prod_terminal_unfinalized": self._environment == "PROD",
                "test_filled_unemitted": self._environment == "TEST",
                "captured_at": floor_utc_millisecond(captured_at),
            }
            self._set_busy_timeout(connection, deadline)
            intent_rows = connection.execute(_INTENTS, parameters).mappings().all()
            self._check_budget(deadline)
            self._set_busy_timeout(connection, deadline)
            fact_rows = connection.execute(_FACT_COUNTS, parameters).mappings().all()
            self._check_budget(deadline)

            intents_by_automation: dict[str, list[WorkerIntentDiagnostics]] = {}
            overflowed: set[str] = set()
            for row in intent_rows:
                intents = intents_by_automation.setdefault(row["automation_id"], [])
                if len(intents) == _MAX_INTENTS_PER_AUTOMATION:
                    overflowed.add(row["automation_id"])
                    continue
                intents.append(
                    WorkerIntentDiagnostics(
                        intent_id=row["idempotency_key"],
                        decision_id=row["decision_id"],
                        process_id=row["process_id"],
                        active=bool(row["is_active"]),
                        state=row["state"],
                        side=row["side"],
                        external_order_id=row["broker_order_id"],
                        quantity_lots=row["quantity_lots"],
                        limit_price=row["limit_price"],
                        requested_amount=row["requested_amount"],
                        executed_lots=row["executed_lots"],
                        executed_amount=row["executed_amount"],
                        executed_price=row["executed_price"],
                        estimated_commission=row["estimated_commission"],
                        executed_commission=row["executed_commission"],
                        currency=row["execution_currency"],
                        fact_execution_id=row["fact_execution_id"],
                        origin_runtime_instance_id=row["origin_runtime_instance_id"],
                        origin_generation=row["origin_generation"],
                        created_at=_as_utc(row["created_at"]),
                        updated_at=_as_utc(row["updated_at"]),
                        dispatch_started_at=_as_utc(row["dispatch_started_at"]),
                        broker_responded_at=_as_utc(row["broker_responded_at"]),
                        executed_at=_as_utc(row["executed_at"]),
                        terminal_at=_as_utc(row["terminal_at"]),
                        execution_facts_emitted_at=_as_utc(row["execution_facts_emitted_at"]),
                    )
                )
            counts = {row["automation_id"]: row for row in fact_rows}
            self._check_budget(deadline)
            rows = tuple(
                WorkerAutomationExecutionDiagnostics(
                    automation_id=record["automation_id"],
                    account_id=record["account_id"],
                    external_instrument_id=record["instrument_id"],
                    fact_instrument_id=record["fact_instrument_id"],
                    lot_size=record["lot_size"],
                    pending_fact_count=(counts.get(record["automation_id"]) or {}).get("pending_count") or 0,
                    failed_fact_count=(counts.get(record["automation_id"]) or {}).get("failed_count") or 0,
                    intents=tuple(intents_by_automation.get(record["automation_id"], ())),
                    history_truncated=record["automation_id"] in overflowed,
                )
                for record in automation_rows
            )
            self._check_budget(deadline)
            return WorkerExecutionDiagnostics(
                observation="OBSERVED",
                reason="OBSERVED",
                broker_id=broker_uuid,
                captured_at=floor_utc_millisecond(captured_at),
                rows=rows,
                truncated=truncated,
            )
        except _BudgetExceeded:
            return WorkerExecutionDiagnostics(reason="BUDGET_EXCEEDED", broker_id=broker_uuid)
        except (sqlite3.OperationalError, OperationalError) as error:
            if monotonic() >= deadline or "locked" in str(error).lower() or "busy" in str(error).lower():
                return WorkerExecutionDiagnostics(reason="BUDGET_EXCEEDED", broker_id=broker_uuid)
            return WorkerExecutionDiagnostics(reason="READ_FAILED", broker_id=broker_uuid)
        except Exception:
            return WorkerExecutionDiagnostics(reason="READ_FAILED", broker_id=broker_uuid)
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.connection.driver_connection.set_progress_handler(None, 0)
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()
            if engine is not None:
                engine.dispose()

    def _database_path(self) -> Path | None:
        try:
            url = make_url(self._database_url)
        except Exception:
            return None
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            return None
        if url.query:
            return None
        try:
            return Path(url.database).resolve(strict=True)
        except OSError:
            return None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _BudgetExceeded
        return remaining

    @classmethod
    def _check_budget(cls, deadline: float) -> None:
        cls._remaining(deadline)

    @classmethod
    def _set_busy_timeout(cls, connection, deadline: float) -> None:
        milliseconds = max(1, min(50, int(cls._remaining(deadline) * 1000)))
        connection.exec_driver_sql(f"PRAGMA busy_timeout={milliseconds}")


class _BudgetExceeded(Exception):
    pass


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)
