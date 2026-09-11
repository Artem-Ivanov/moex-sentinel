"""Atomic persistence for configured broker API/account scopes."""

from typing import Never

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.user_brokers import (
    UserBroker,
    UserBrokerConstraintError,
    UserBrokerDraft,
    UserBrokerDuplicateError,
    UserBrokerNotFoundError,
    UserBrokerState,
)
from moex_sentinel.storage.database import session_scope
from moex_sentinel.storage.models.user_brokers import UserBrokerModel


def _record(model: UserBrokerModel) -> UserBroker:
    return UserBroker(
        id=model.id,
        api_slug=model.api_slug,
        display_name=model.display_name,
        environment=model.environment,
        fqdn=model.fqdn,
        settings=dict(model.settings),
        external_account_id=model.external_account_id,
        state=UserBrokerState(model.state),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class UserBrokerRepository:
    """Repository for configured user-broker scopes."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def list(self) -> list[UserBroker]:
        with session_scope(self._factory) as session:
            statement = select(UserBrokerModel).order_by(UserBrokerModel.created_at, UserBrokerModel.id)
            return [_record(model) for model in session.scalars(statement)]

    def get(self, user_broker_id: str) -> UserBroker:
        with session_scope(self._factory) as session:
            return _record(self._get_model(session, user_broker_id))

    def create(self, draft: UserBrokerDraft, *, record_id: str | None = None) -> UserBroker:
        values: dict[str, object] = {}
        if record_id is not None:
            values["id"] = record_id
        try:
            with session_scope(self._factory) as session:
                model = UserBrokerModel(
                    **values,
                    api_slug=draft.api_slug,
                    display_name=draft.display_name,
                    environment=draft.environment,
                    fqdn=draft.fqdn,
                    settings=dict(draft.settings),
                    external_account_id=draft.external_account_id,
                    state=draft.state.value,
                )
                session.add(model)
                session.flush()
                return _record(model)
        except IntegrityError as error:
            self._raise_constraint_error(error)

    def replace(self, user_broker_id: str, draft: UserBrokerDraft) -> UserBroker:
        try:
            with session_scope(self._factory) as session:
                model = self._get_model(session, user_broker_id)
                model.api_slug = draft.api_slug
                model.display_name = draft.display_name
                model.environment = draft.environment
                model.fqdn = draft.fqdn
                model.settings = dict(draft.settings)
                model.external_account_id = draft.external_account_id
                model.state = draft.state.value
                session.flush()
                return _record(model)
        except IntegrityError as error:
            self._raise_constraint_error(error)

    def disable(self, user_broker_id: str) -> UserBroker:
        with session_scope(self._factory) as session:
            model = self._get_model(session, user_broker_id)
            model.state = UserBrokerState.DISABLED.value
            session.flush()
            return _record(model)

    @staticmethod
    def _get_model(session: Session, user_broker_id: str) -> UserBrokerModel:
        model = session.get(UserBrokerModel, user_broker_id)
        if model is None:
            raise UserBrokerNotFoundError(user_broker_id)
        return model

    @staticmethod
    def _raise_constraint_error(error: IntegrityError) -> Never:
        safe_database_message = str(error.orig).lower()
        if "unique" in safe_database_message:
            raise UserBrokerDuplicateError("Duplicate user-broker identity.") from None
        raise UserBrokerConstraintError("User-broker persistence constraint failed.") from None
