import io
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from moex_sentinel.api.app import create_app
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models import Base
from sentinel_contracts.audit import (
    JsonAuditFormatter,
    SuccessfulHeartbeatFilter,
    audit_event,
    business_process,
    current_process_id,
)


def test_business_process_inherits_and_resets_context() -> None:
    assert current_process_id() is None

    with business_process() as outer:
        UUID(outer)
        assert current_process_id() == outer
        with business_process(process_id=outer):
            assert current_process_id() == outer
        assert current_process_id() == outer

    assert current_process_id() is None


def test_business_process_resets_context_after_exception() -> None:
    with pytest.raises(RuntimeError), business_process():
        raise RuntimeError("synthetic failure")

    assert current_process_id() is None


def test_json_formatter_emits_safe_exact_business_event() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonAuditFormatter("worker"))
    logger = logging.getLogger("test.audit")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with business_process(process_id="9c8a54fd-0000-4000-8000-000000000001"):
        audit_event(
            logger,
            "STRATEGY_DECISION_MADE",
            "Decision calculated",
            automation_id="automation-1",
            data={"price": Decimal("247.4500")},
        )

    event = json.loads(output.getvalue())
    assert event["service"] == "worker"
    assert event["process_id"] == "9c8a54fd-0000-4000-8000-000000000001"
    assert event["stage"] == "STRATEGY_DECISION_MADE"
    assert event["automation_id"] == "automation-1"
    assert event["data"] == {"price": "247.4500"}
    assert event["timestamp"].endswith("Z")
    assert "token" not in event


def test_successful_internal_heartbeat_does_not_emit_info_http_audit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'heartbeat.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    application = create_app(database_url=database_url)

    with caplog.at_level(logging.INFO, logger="moex_sentinel.api.app"), TestClient(application) as client:
        response = client.post(
            "/internal/automaton/heartbeats",
            json={
                "worker_id": "worker-1",
                "occurred_at": datetime(2026, 8, 7, 12, tzinfo=UTC).isoformat(),
            },
        )

    assert response.status_code == 200
    heartbeat_records = [
        record
        for record in caplog.records
        if getattr(record, "data", {}).get("path") == "/internal/automaton/heartbeats"
    ]
    assert heartbeat_records == []


def test_successful_heartbeat_filter_keeps_failures_and_other_http_logs() -> None:
    filter_ = SuccessfulHeartbeatFilter()
    successful = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: POST http://backend:8000/internal/automaton/heartbeats "HTTP/1.1 200 OK"',
        (),
        None,
    )
    failed = logging.LogRecord(
        "httpx",
        logging.WARNING,
        __file__,
        1,
        "Heartbeat request failed",
        (),
        None,
    )
    other = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: GET /commands", (), None)

    assert filter_.filter(successful) is False
    assert filter_.filter(failed) is True
    assert filter_.filter(other) is True
