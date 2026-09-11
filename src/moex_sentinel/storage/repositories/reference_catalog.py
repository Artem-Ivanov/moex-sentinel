"""Atomic persistence for a user-broker-scoped instrument catalog."""

from datetime import datetime
from typing import Never

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.instrument_catalog import (
    CatalogInstrumentNotFoundError,
    UserBrokerCatalogConstraintError,
    UserBrokerCatalogInstrument,
    UserBrokerCatalogInstrumentDraft,
    UserBrokerCatalogReconciliationResult,
    UserBrokerCatalogSyncState,
)
from moex_sentinel.storage.database import session_scope
from moex_sentinel.storage.models.reference_data import (
    BrokerInstrumentModel,
    InstrumentSyncStateModel,
)


def _record(model: BrokerInstrumentModel) -> UserBrokerCatalogInstrument:
    return UserBrokerCatalogInstrument(
        id=model.id,
        user_broker_id=model.user_broker_id,
        external_instrument_id=model.external_instrument_id,
        external_identifiers=dict(model.external_identifiers),
        ticker=model.ticker,
        name=model.name,
        instrument_type=model.instrument_type,
        class_code=model.class_code,
        currency=model.currency,
        lot_size=model.lot_size,
        min_price_increment=model.min_price_increment,
        api_trade_available=model.api_trade_available,
        is_active=model.is_active,
        is_selected=model.is_selected,
        first_seen_at=model.first_seen_at,
        last_seen_at=model.last_seen_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _sync_record(model: InstrumentSyncStateModel | None, user_broker_id: str) -> UserBrokerCatalogSyncState:
    if model is None:
        return UserBrokerCatalogSyncState(
            user_broker_id=user_broker_id,
            status="NOT_STARTED",
            last_attempt_at=None,
            last_success_at=None,
            safe_error=None,
            reconciliation_required=True,
        )
    return UserBrokerCatalogSyncState(
        user_broker_id=model.user_broker_id,
        status=model.status,
        last_attempt_at=model.last_attempt_at,
        last_success_at=model.last_success_at,
        safe_error=model.safe_error,
        reconciliation_required=model.reconciliation_required,
    )


class ReferenceCatalogRepository:
    """Repository for the user-broker-scoped reference catalog."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def reconcile(
        self,
        user_broker_id: str,
        snapshot: tuple[UserBrokerCatalogInstrumentDraft, ...],
        synchronized_at: datetime,
    ) -> UserBrokerCatalogReconciliationResult:
        try:
            with session_scope(self._factory) as session:
                existing = {
                    item.external_instrument_id: item
                    for item in session.scalars(
                        select(BrokerInstrumentModel).where(BrokerInstrumentModel.user_broker_id == user_broker_id)
                    )
                }
                added = 0
                updated = 0
                seen: set[str] = set()
                for draft in snapshot:
                    seen.add(draft.external_instrument_id)
                    model = existing.get(draft.external_instrument_id)
                    if model is None:
                        session.add(
                            BrokerInstrumentModel(
                                user_broker_id=user_broker_id,
                                external_instrument_id=draft.external_instrument_id,
                                external_identifiers=dict(draft.external_identifiers),
                                ticker=draft.ticker,
                                name=draft.name,
                                instrument_type=draft.instrument_type,
                                class_code=draft.class_code,
                                currency=draft.currency,
                                lot_size=draft.lot_size,
                                min_price_increment=draft.min_price_increment,
                                api_trade_available=draft.api_trade_available,
                                is_active=True,
                                is_selected=False,
                                first_seen_at=synchronized_at,
                                last_seen_at=synchronized_at,
                            )
                        )
                        added += 1
                        continue
                    self._update_model(model, draft, synchronized_at)
                    updated += 1
                deactivated = 0
                for external_instrument_id, model in existing.items():
                    if external_instrument_id not in seen and model.is_active:
                        model.is_active = False
                        deactivated += 1
                state = session.get(InstrumentSyncStateModel, user_broker_id)
                if state is None:
                    state = InstrumentSyncStateModel(user_broker_id=user_broker_id, status="SUCCESS")
                    session.add(state)
                state.status = "SUCCESS"
                state.last_attempt_at = synchronized_at
                state.last_success_at = synchronized_at
                state.safe_error = None
                state.reconciliation_required = False
                session.flush()
                return UserBrokerCatalogReconciliationResult(
                    user_broker_id=user_broker_id,
                    added=added,
                    updated=updated,
                    deactivated=deactivated,
                    synchronized_at=synchronized_at,
                )
        except IntegrityError as error:
            self._raise_constraint_error(error)

    def list(
        self,
        user_broker_id: str,
        *,
        include_inactive: bool,
    ) -> tuple[UserBrokerCatalogInstrument, ...]:
        with session_scope(self._factory) as session:
            statement = select(BrokerInstrumentModel).where(BrokerInstrumentModel.user_broker_id == user_broker_id)
            if not include_inactive:
                statement = statement.where(BrokerInstrumentModel.is_active.is_(True))
            statement = statement.order_by(
                BrokerInstrumentModel.ticker,
                BrokerInstrumentModel.name,
                BrokerInstrumentModel.id,
            )
            return tuple(_record(model) for model in session.scalars(statement))

    def get(self, user_broker_id: str, instrument_id: str) -> UserBrokerCatalogInstrument:
        with session_scope(self._factory) as session:
            return _record(self._get_model(session, user_broker_id, instrument_id))

    def find_by_external_instrument_id(
        self,
        user_broker_id: str,
        external_instrument_id: str,
    ) -> UserBrokerCatalogInstrument | None:
        with session_scope(self._factory) as session:
            model = session.scalar(
                select(BrokerInstrumentModel).where(
                    BrokerInstrumentModel.user_broker_id == user_broker_id,
                    BrokerInstrumentModel.external_instrument_id == external_instrument_id,
                )
            )
            return None if model is None else _record(model)

    def set_selected(
        self,
        user_broker_id: str,
        instrument_id: str,
        selected: bool,
    ) -> UserBrokerCatalogInstrument:
        with session_scope(self._factory) as session:
            model = self._get_model(session, user_broker_id, instrument_id)
            model.is_selected = selected
            session.flush()
            return _record(model)

    def sync_state(self, user_broker_id: str) -> UserBrokerCatalogSyncState:
        with session_scope(self._factory) as session:
            return _sync_record(session.get(InstrumentSyncStateModel, user_broker_id), user_broker_id)

    def mark_failed(self, user_broker_id: str, attempted_at: datetime, safe_error: str) -> None:
        try:
            with session_scope(self._factory) as session:
                state = session.get(InstrumentSyncStateModel, user_broker_id)
                if state is None:
                    state = InstrumentSyncStateModel(user_broker_id=user_broker_id, status="FAILED")
                    session.add(state)
                state.status = "FAILED"
                state.last_attempt_at = attempted_at
                state.safe_error = safe_error
                state.reconciliation_required = True
        except IntegrityError as error:
            self._raise_constraint_error(error)

    @staticmethod
    def _update_model(
        model: BrokerInstrumentModel,
        draft: UserBrokerCatalogInstrumentDraft,
        synchronized_at: datetime,
    ) -> None:
        model.external_identifiers = dict(draft.external_identifiers)
        model.ticker = draft.ticker
        model.name = draft.name
        model.instrument_type = draft.instrument_type
        model.class_code = draft.class_code
        model.currency = draft.currency
        model.lot_size = draft.lot_size
        model.min_price_increment = draft.min_price_increment
        model.api_trade_available = draft.api_trade_available
        model.is_active = True
        model.last_seen_at = synchronized_at

    @staticmethod
    def _get_model(session: Session, user_broker_id: str, instrument_id: str) -> BrokerInstrumentModel:
        model = session.scalar(
            select(BrokerInstrumentModel).where(
                BrokerInstrumentModel.user_broker_id == user_broker_id,
                BrokerInstrumentModel.id == instrument_id,
            )
        )
        if model is None:
            raise CatalogInstrumentNotFoundError(instrument_id)
        return model

    @staticmethod
    def _raise_constraint_error(_error: IntegrityError) -> Never:
        raise UserBrokerCatalogConstraintError("User-broker catalog persistence constraint failed.") from None
