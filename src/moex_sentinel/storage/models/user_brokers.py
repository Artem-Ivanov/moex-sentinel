"""Persistence model for configured broker API/account scopes."""

from sqlalchemy import JSON, CheckConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from moex_sentinel.storage.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin


class UserBrokerModel(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One selected code-owned broker API and external account."""

    __tablename__ = "user_brokers"
    __table_args__ = (
        CheckConstraint("environment IN ('TEST', 'PROD')", name="ck_user_brokers_environment"),
        CheckConstraint(
            "state IN ('DRAFT', 'ACTIVE', 'DISABLED', 'ERROR')",
            name="ck_user_brokers_state",
        ),
        CheckConstraint(
            "state != 'ACTIVE' OR (external_account_id IS NOT NULL AND length(trim(external_account_id)) > 0)",
            name="ck_user_brokers_active_account",
        ),
        Index(
            "uq_user_brokers_api_environment_account",
            "api_slug",
            "environment",
            "external_account_id",
            unique=True,
            sqlite_where=text("external_account_id IS NOT NULL"),
        ),
    )

    api_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    environment: Mapped[str] = mapped_column(String(8), nullable=False)
    fqdn: Mapped[str] = mapped_column(String(255), nullable=False)
    settings: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), nullable=False)
