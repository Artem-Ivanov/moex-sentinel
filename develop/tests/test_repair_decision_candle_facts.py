"""Synthetic recovery tests; no live broker or Core calls."""

import copy
import importlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import (
    AutomationStatus,
    AutomationStatusesResult,
    FactBatchResult,
    FactGroupAcknowledgement,
)

sys.path.insert(0, str(Path(__file__).parents[2]))
from tests.contracts.trading_facts_helpers import all_envelopes

repair = importlib.import_module("develop.scripts.repair_decision_candle_facts")


class CoreStub:
    def __init__(self, fact):
        self.status = AutomationStatus(
            automation_id=fact.automation_id,
            user_broker_id=fact.user_broker_id,
            state=AutomationState.HOLD,
            revision=1,
            last_sequence_number=3,
            resume_requested=False,
        )
        self.published = []
        self.accepted = {}
        self.lose_ack = False

    def automation_statuses(self, ids):
        assert ids == [self.status.automation_id]
        return AutomationStatusesResult(automations=(self.status,))

    def publish_facts(self, facts):
        self.published.extend(facts)
        for fact in facts:
            assert "decision_candle_at" not in fact.payload.strategy_snapshot
            if fact.sequence_number in self.accepted:
                assert self.accepted[fact.sequence_number] == fact
            self.accepted[fact.sequence_number] = fact
        self.status = self.status.model_copy(update={"last_sequence_number": max(self.accepted)})
        if self.lose_ack:
            self.lose_ack = False
            raise httpx.ReadTimeout("synthetic sensitive details must not escape")
        return FactBatchResult(
            results=(
                FactGroupAcknowledgement(
                    automation_id=self.status.automation_id,
                    accepted_through_sequence=self.status.last_sequence_number,
                    current_revision=self.status.revision,
                    accepted_event_ids=tuple(fact.event_id for fact in facts),
                ),
            )
        )


@pytest.fixture
def scenario(tmp_path):
    database = tmp_path / "worker.sqlite"
    fact = all_envelopes()[3]
    payload = fact.payload.model_dump(mode="json")
    snapshot = copy.deepcopy(payload["strategy_snapshot"])
    payload["strategy_snapshot"]["decision_candle_at"] = "2026-08-13T10:00:00.000Z"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE worker_runs (worker_id TEXT PRIMARY KEY, clean_shutdown BOOLEAN);
            INSERT INTO worker_runs VALUES ('synthetic-worker', 1);
            CREATE TABLE cached_automations (automation_id TEXT PRIMARY KEY, state TEXT, revision INTEGER,
                last_sequence_number INTEGER, resume_requested BOOLEAN);
            CREATE TABLE trade_decisions (id TEXT PRIMARY KEY, automation_id TEXT, strategy_snapshot TEXT);
            CREATE TABLE fact_outbox (event_id TEXT PRIMARY KEY, user_broker_id TEXT, automation_id TEXT,
                sequence_number INTEGER, expected_revision INTEGER, fact_kind TEXT, payload TEXT,
                safe_message TEXT, occurred_at TEXT, delivery_state TEXT, next_retry_at TEXT);
        """
        )
        db.execute("INSERT INTO cached_automations VALUES (?, 'HOLD', 1, 4, 0)", (str(fact.automation_id),))
        db.execute(
            "INSERT INTO trade_decisions VALUES (?, ?, ?)",
            (str(fact.payload.decision_id), str(fact.automation_id), json.dumps(snapshot)),
        )
        db.execute(
            "INSERT INTO fact_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FAILED', NULL)",
            (
                str(fact.event_id),
                str(fact.user_broker_id),
                str(fact.automation_id),
                fact.sequence_number,
                fact.expected_revision,
                fact.fact_kind.value,
                json.dumps(payload),
                fact.safe_message,
                fact.occurred_at.isoformat(),
            ),
        )
    return database, CoreStub(fact)


def rows(database, table="fact_outbox"):
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def test_default_dry_run_does_not_change_database_or_publish(scenario):
    database, client = scenario
    before = database.read_bytes()
    result = repair.repair_database(database, client)
    assert result == {"groups": 1, "corrections": 1, "queued_facts": 1, "acknowledged_facts": 0, "reconciled_groups": 0}
    assert database.read_bytes() == before
    assert client.published == []


def test_apply_preserves_fact_identity_and_reconciles_authoritative_hold(scenario):
    database, client = scenario
    before = rows(database)[0]
    result = repair.repair_database(database, client, apply=True)
    sent = client.published[0].model_dump(mode="json")
    for field in (
        "event_id",
        "automation_id",
        "user_broker_id",
        "sequence_number",
        "expected_revision",
        "safe_message",
    ):
        assert sent[field] == before[field]
    expected_payload = json.loads(before["payload"])
    del expected_payload["strategy_snapshot"]["decision_candle_at"]
    assert sent["payload"] == expected_payload
    assert rows(database) == []
    assert result["acknowledged_facts"] == 1
    cached = rows(database, "cached_automations")[0]
    assert (cached["state"], cached["last_sequence_number"], cached["resume_requested"]) == ("HOLD", 4, 0)
    assert repair.repair_database(database, client, apply=True)["groups"] == 0


@pytest.mark.parametrize("conflict", ["snapshot", "accepted", "gap", "revision", "active", "state_fact"])
def test_conflicts_refuse_before_mutation(scenario, conflict):
    database, client = scenario
    with sqlite3.connect(database) as db:
        if conflict == "snapshot":
            db.execute("UPDATE trade_decisions SET strategy_snapshot = '{}' ")
        elif conflict == "accepted":
            client.status = client.status.model_copy(update={"last_sequence_number": 4})
        elif conflict == "gap":
            db.execute("UPDATE fact_outbox SET sequence_number = 5")
        elif conflict == "revision":
            client.status = client.status.model_copy(update={"revision": 2})
        elif conflict == "active":
            db.execute("UPDATE worker_runs SET clean_shutdown = 0")
        elif conflict == "state_fact":
            state = all_envelopes()[0].model_dump(mode="json")
            db.execute(
                "INSERT INTO fact_outbox SELECT ?, user_broker_id, automation_id, 5, 1, ?, ?, '', "
                "occurred_at, 'FAILED', NULL FROM fact_outbox",
                (state["event_id"], state["fact_kind"], json.dumps(state["payload"])),
            )
    before = database.read_bytes()
    with pytest.raises(repair.RepairRefused):
        repair.repair_database(database, client, apply=True)
    assert rows(database)
    assert database.read_bytes() == before
    assert client.published == []


def test_lost_ack_retry_republishes_identical_corrected_fact(scenario):
    database, client = scenario
    client.lose_ack = True
    with pytest.raises(httpx.ReadTimeout):
        repair.repair_database(database, client, apply=True)
    assert "decision_candle_at" not in json.loads(rows(database)[0]["payload"])["strategy_snapshot"]
    assert repair.repair_database(database, client)["corrections"] == 0
    result = repair.repair_database(database, client, apply=True)
    assert result["acknowledged_facts"] == 1
    assert len(client.accepted) == 1
    assert client.published[0] == client.published[1]
    assert rows(database) == []


def test_interrupted_before_publish_can_resume(scenario, monkeypatch):
    database, client = scenario
    publish = client.publish_facts
    monkeypatch.setattr(client, "publish_facts", lambda facts: (_ for _ in ()).throw(httpx.ConnectError("synthetic")))
    with pytest.raises(httpx.ConnectError):
        repair.repair_database(database, client, apply=True)
    monkeypatch.setattr(client, "publish_facts", publish)
    assert repair.repair_database(database, client, apply=True)["acknowledged_facts"] == 1


def test_cli_sanitizes_errors_and_defaults_to_dry_run(scenario, monkeypatch):
    database, client = scenario
    monkeypatch.setattr(repair, "CoreClient", lambda http: client)
    output = io.StringIO()
    assert repair.main(["--database", str(database), "--core-url", "http://localhost:8000"], output=output) == 0
    assert json.loads(output.getvalue())["mode"] == "dry-run"
    assert client.published == []
    client.lose_ack = True
    output = io.StringIO()
    assert (
        repair.main(["--database", str(database), "--core-url", "http://localhost:8000", "--apply"], output=output) == 1
    )
    assert json.loads(output.getvalue()) == {"error": "ReadTimeout"}


@pytest.mark.parametrize("state", [AutomationState.HOLD, AutomationState.CLOSED])
def test_authoritative_user_state_is_preserved(scenario, state):
    database, client = scenario
    client.status = client.status.model_copy(update={"state": state, "revision": 2})
    with sqlite3.connect(database) as db:
        db.execute("UPDATE fact_outbox SET expected_revision = 2")
    repair.repair_database(database, client, apply=True)
    cached = rows(database, "cached_automations")[0]
    assert (cached["state"], cached["revision"], cached["resume_requested"]) == (state.value, 2, 0)


def test_incomplete_ack_keeps_queue_for_identical_retry(scenario, monkeypatch):
    database, client = scenario
    publish = client.publish_facts

    def incomplete(facts):
        response = publish(facts)
        ack = response.results[0].model_copy(update={"accepted_event_ids": ()})
        return FactBatchResult(results=(ack,))

    monkeypatch.setattr(client, "publish_facts", incomplete)
    with pytest.raises(repair.RepairRefused, match="CORE_ACK_CONFLICT"):
        repair.repair_database(database, client, apply=True)
    assert len(rows(database)) == 1
    monkeypatch.setattr(client, "publish_facts", publish)
    assert repair.repair_database(database, client, apply=True)["acknowledged_facts"] == 1


def test_multiple_groups_resume_after_partial_delivery(scenario):
    database, first = scenario
    second_id = "00000000-0000-4000-8000-000000000103"
    second_event = "00000000-0000-4000-8000-000000000113"
    second_decision = "00000000-0000-4000-8000-000000000106"
    original = rows(database)[0]
    payload = json.loads(original["payload"])
    payload["decision_id"] = second_decision
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO cached_automations SELECT ?, state, revision, last_sequence_number, "
            "resume_requested FROM cached_automations",
            (second_id,),
        )
        db.execute(
            "INSERT INTO trade_decisions SELECT ?, ?, strategy_snapshot FROM trade_decisions",
            (second_decision, second_id),
        )
        db.execute(
            "INSERT INTO fact_outbox SELECT ?, user_broker_id, ?, sequence_number, expected_revision, "
            "fact_kind, ?, safe_message, occurred_at, delivery_state, next_retry_at FROM fact_outbox",
            (second_event, second_id, json.dumps(payload)),
        )
    second = CoreStub(all_envelopes()[3].model_copy(update={"automation_id": UUID(second_id)}))
    second.lose_ack = True

    class GroupsClient:
        def automation_statuses(self, ids):
            return AutomationStatusesResult(
                automations=tuple(client.status for client in (first, second) if client.status.automation_id in ids)
            )

        def publish_facts(self, facts):
            target = first if facts[0].automation_id == first.status.automation_id else second
            return target.publish_facts(facts)

    client = GroupsClient()
    with pytest.raises(httpx.ReadTimeout):
        repair.repair_database(database, client, apply=True)
    assert len(rows(database)) == 1
    result = repair.repair_database(database, client, apply=True)
    assert result["groups"] == result["acknowledged_facts"] == result["reconciled_groups"] == 1
    assert rows(database) == []
    assert len(first.published) == 1
    assert len(second.published) == 2
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name = ?", (repair.JOURNAL,)).fetchall() == []


def test_sqlalchemy_utc_storage_timestamp_is_restored_without_payload_changes(scenario):
    from sqlalchemy import bindparam, create_engine, text

    from trading_automaton.storage.models import UTCDateTime

    database, client = scenario
    occurred_at = all_envelopes()[3].occurred_at
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE fact_outbox SET occurred_at = :occurred_at").bindparams(
                    bindparam("occurred_at", type_=UTCDateTime())
                ),
                {"occurred_at": occurred_at},
            )
    finally:
        engine.dispose()
    assert rows(database)[0]["occurred_at"] == "2026-08-13 10:00:00.123000"
    before = database.read_bytes()
    assert repair.repair_database(database, client)["corrections"] == 1
    assert database.read_bytes() == before
    assert repair.repair_database(database, client, apply=True)["acknowledged_facts"] == 1
    assert client.published[0].occurred_at == occurred_at
    expected_payload = all_envelopes()[3].payload.model_dump(mode="json")
    assert client.published[0].payload.model_dump(mode="json") == expected_payload
