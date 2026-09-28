"""Frozen W2a selector algorithm, independent of repository implementation."""

from datetime import datetime, timedelta
from itertools import groupby
from sqlite3 import Connection as SQLiteConnection
from typing import cast

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import FactKind, PositionLotSource
from trading_automaton.storage.models import CachedAutomationModel, FactOutboxModel


def select_old_batch(factory: sessionmaker[Session], limit: int, *, now: datetime, deadline_ms: int) -> list[str]:
    """Return IDs using the pre-W2b normal/bootstrap selection algorithm."""
    if limit <= 0:
        return []
    with factory() as session:
        connection = session.connection()
        driver = cast(SQLiteConnection, connection.connection.driver_connection)
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
        potential_bootstrap = session.scalar(
            select(FactOutboxModel.event_id)
            .join(CachedAutomationModel, CachedAutomationModel.automation_id == FactOutboxModel.automation_id)
            .where(
                FactOutboxModel.delivery_state == "PENDING",
                FactOutboxModel.fact_kind == FactKind.AUTOMATION_STATE_CHANGED.value,
                CachedAutomationModel.bootstrap_position_cycle_id.is_not(None),
            )
            .limit(1)
        )
        if potential_bootstrap is None:
            blocked = (
                select(
                    FactOutboxModel.automation_id,
                    func.min(FactOutboxModel.sequence_number).label("sequence_number"),
                )
                .where(FactOutboxModel.delivery_state == "PENDING", FactOutboxModel.next_retry_at > now)
                .group_by(FactOutboxModel.automation_id)
                .subquery()
            )
            eligible = (
                select(FactOutboxModel)
                .outerjoin(blocked, blocked.c.automation_id == FactOutboxModel.automation_id)
                .where(
                    FactOutboxModel.delivery_state == "PENDING",
                    or_(
                        blocked.c.sequence_number.is_(None),
                        FactOutboxModel.sequence_number < blocked.c.sequence_number,
                    ),
                )
            )
            rows = session.scalars(
                eligible.order_by(FactOutboxModel.occurred_at, FactOutboxModel.event_id).limit(limit)
            ).all()
            if len(rows) < limit:
                oldest = min((row.created_at for row in rows), default=None)
                if oldest is None or now < oldest + timedelta(milliseconds=deadline_ms):
                    return []
            return [row.event_id for row in rows]

        # The old bootstrap path deliberately materialized every pending ORM row.
        pending = session.scalars(
            select(FactOutboxModel)
            .where(FactOutboxModel.delivery_state == "PENDING")
            .order_by(FactOutboxModel.automation_id, FactOutboxModel.sequence_number)
        ).all()
        bootstraps = {
            cached.automation_id: cached
            for cached in session.scalars(
                select(CachedAutomationModel).where(CachedAutomationModel.bootstrap_position_cycle_id.is_not(None))
            )
        }
        units: list[list[FactOutboxModel]] = []
        for automation_id, grouped_rows in groupby(pending, key=lambda row: row.automation_id):
            rows = list(grouped_rows)
            cached = bootstraps.get(automation_id)
            index = 0
            while index < len(rows):
                quartet = rows[index : index + 4]
                valid = (
                    cached is not None
                    and len(quartet) == 4
                    and [row.fact_kind for row in quartet]
                    == [
                        FactKind.POSITION_CYCLE_UPDATED.value,
                        FactKind.POSITION_LOT_OPENED.value,
                        FactKind.TRADE_AUDIT_RECORDED.value,
                        FactKind.AUTOMATION_STATE_CHANGED.value,
                    ]
                    and [row.sequence_number for row in quartet]
                    == list(range(quartet[0].sequence_number, quartet[0].sequence_number + 4))
                    and quartet[0].payload["position_cycle_id"] == cached.bootstrap_position_cycle_id
                    and quartet[1].payload["position_cycle_id"] == cached.bootstrap_position_cycle_id
                    and quartet[1].payload["position_lot_id"] == cached.bootstrap_position_lot_id
                    and quartet[1].payload["source"] == PositionLotSource.BROKER_POSITION_BOOTSTRAP.value
                    and quartet[2].payload["stage"] == "BOOTSTRAP_POSITION_ADOPTED"
                    and quartet[3].payload["state"] == AutomationState.IN_WORK.value
                )
                if valid:
                    units.append(quartet)
                    index += 4
                else:
                    units.append([rows[index]])
                    index += 1

        eligible_units: list[list[FactOutboxModel]] = []
        blocked_automations: set[str] = set()
        for unit in units:
            automation_id = unit[0].automation_id
            if automation_id in blocked_automations:
                continue
            if any(row.next_retry_at is not None and row.next_retry_at > now for row in unit):
                blocked_automations.add(automation_id)
                continue
            eligible_units.append(unit)
        eligible_count = sum(len(unit) for unit in eligible_units)
        oldest = min((row.created_at for unit in eligible_units for row in unit), default=None)
        due = oldest is not None and now >= oldest + timedelta(milliseconds=deadline_ms)
        if eligible_count < limit and not due:
            return []

        selected: list[FactOutboxModel] = []
        selected_bootstraps: set[str] = set()
        for unit in sorted(eligible_units, key=lambda item: (item[0].occurred_at, item[0].event_id)):
            automation_id = unit[0].automation_id
            if automation_id in selected_bootstraps:
                continue
            selected.extend(unit)
            if len(unit) == 4:
                selected_bootstraps.add(automation_id)
            if len(selected) >= limit:
                break
        selected.sort(key=lambda row: (row.occurred_at, row.event_id))
        return [row.event_id for row in selected]
