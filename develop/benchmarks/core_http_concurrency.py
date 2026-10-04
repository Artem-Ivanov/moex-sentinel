"""Bounded A1-only HTTP comparison using identical current SQL and synthetic facts."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from develop.benchmarks.pg_metrics import Profile, instrument_database
from develop.benchmarks.pg_server import CoreServer
from develop.benchmarks.postgresql import SeedSession, isolated_database
from develop.benchmarks.runtime import Harness, require
from develop.benchmarks.synthetic import START, commands, identifier
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.services.trading_fact_mapping import TradingFactMapper
from moex_sentinel.storage.models import AutomationEventModel, TradingAutomationModel
from moex_sentinel.storage.repositories.trading_facts_uow import TradingFactsUnitOfWork
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationStateChangedEnvelope, AutomationStateChangedPayload, FactKind


def facts_for(values):
    return tuple(
        AutomationStateChangedEnvelope(
            event_id=identifier(f"http-hold-{index}"),
            user_broker_id=value.user_broker_id,
            automation_id=value.automation_id,
            sequence_number=1,
            expected_revision=1,
            safe_message="Synthetic A1 benchmark hold",
            occurred_at=START,
            fact_kind=FactKind.AUTOMATION_STATE_CHANGED,
            payload=AutomationStateChangedPayload(
                state=AutomationState.HOLD,
                suspended_from_state=AutomationState.IN_WORK,
                hold_reason="synthetic hold",
                closed_at=None,
            ),
        )
        for index, value in enumerate(values)
    )


async def workload(url, facts, requests, concurrency, samples):
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=30, trust_env=False) as client:

        async def request(index):
            fact = facts[index % len(facts)]
            sample = {"index": index, "state": "failed", "elapsed_ms": None, "status": None, "response": None}
            started = None
            try:
                async with semaphore:
                    started = perf_counter()
                    response = await client.post(
                        "/internal/automation-facts", json={"facts": [fact.model_dump(mode="json")]}
                    )
                    sample["status"] = response.status_code
                    sample["response_text"] = response.text
                    sample["response"] = response.json()
                    require(response.status_code == 200, "Synthetic request failed")
                    require(not sample["response"]["failures"], "Synthetic fact rejected")
                    require(
                        sample["response"]["results"][0]["accepted_event_ids"] == [str(fact.event_id)], "ACK mismatch"
                    )
                    sample["state"] = "passed"
                    return True
            except asyncio.CancelledError:
                sample["state"] = "cancelled"
                sample["exception_type"] = "CancelledError"
                return False
            except Exception as error:
                sample["exception_type"] = type(error).__name__
                return False
            finally:
                if started is not None:
                    sample["elapsed_ms"] = (perf_counter() - started) * 1000
                samples.append(sample)

        tasks = [asyncio.create_task(request(index)) for index in range(requests)]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            # Keep the client open until every scheduled request has a terminal receipt.
            await asyncio.gather(*tasks, return_exceptions=True)
            recorded = {sample["index"] for sample in samples}
            for index in range(requests):
                if index not in recorded:
                    # Cancellation before the coroutine starts cannot execute its finally.
                    samples.append(
                        {
                            "index": index,
                            "state": "cancelled",
                            "elapsed_ms": None,
                            "status": None,
                            "response": None,
                            "exception_type": "CancelledError",
                        }
                    )
        require(
            all(outcome is True for outcome in outcomes), "Synthetic workload contains failed or cancelled requests"
        )


def measure(database_url, mode, count, requests, concurrency, receipt):
    with isolated_database(database_url) as engine:
        values = commands(count)
        seed_factory = sessionmaker(engine, class_=SeedSession, expire_on_commit=False)
        # Reuse the existing parent-before-child benchmark seed unchanged.
        Harness.seed_core(SimpleNamespace(values=values, core_factory=seed_factory))
        profile = Profile()
        session_class, restore = instrument_database(engine, profile)
        factory = sessionmaker(engine, class_=session_class, expire_on_commit=False)
        ingress = TradingFactIngressService(
            lambda: TradingFactsUnitOfWork(factory), TradingFactMapper(), now=lambda: START
        )
        server = CoreServer(ingress, profile, execution_mode=mode)
        facts = facts_for(values)
        started = None
        try:
            server.start()
            asyncio.run(workload(server.url, facts, max(count, concurrency), concurrency, receipt["warmup_samples"]))
            profile.reset()
            started = perf_counter()
            asyncio.run(workload(server.url, facts, requests, concurrency, receipt["samples"]))
        finally:
            if started is not None:
                receipt["elapsed_ms"] = (perf_counter() - started) * 1000
            try:
                server.close()
            finally:
                primary_error = sys.exception()
                receipt["persisted_events"] = None
                receipt["final_automations"] = None
                receipt["final_counts_verified"] = False
                try:
                    # Query actual state after server/pool drain, even after lost responses.
                    with factory() as session:
                        receipt["persisted_events"] = session.scalar(
                            select(func.count()).select_from(AutomationEventModel)
                        )
                        states = session.execute(
                            select(
                                TradingAutomationModel.id,
                                TradingAutomationModel.revision,
                                TradingAutomationModel.last_sequence_number,
                            ).order_by(TradingAutomationModel.id)
                        ).all()
                        receipt["final_automations"] = [
                            {"id": row.id, "revision": row.revision, "last_sequence_number": row.last_sequence_number}
                            for row in states
                        ]
                    receipt["final_counts_verified"] = (
                        receipt["persisted_events"] == count
                        and len(states) == count
                        and {(row.revision, row.last_sequence_number) for row in states} == {(2, 1)}
                    )
                except Exception as error:
                    receipt["diagnostic_error_type"] = type(error).__name__
                    if primary_error is None:
                        raise
                finally:
                    try:
                        receipt["profile"] = profile.report()
                        receipt["raw_profile_samples"] = profile.samples
                        receipt["scheduled_requests"] = len(receipt["samples"])
                        receipt["successful_requests"] = sum(
                            sample["state"] == "passed" for sample in receipt["samples"]
                        )
                    finally:
                        restore()
        require(receipt["final_counts_verified"], "Replay altered final persisted events/revisions")
        receipt["requests_per_second"] = receipt["successful_requests"] * 1000 / receipt["elapsed_ms"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--mode", choices=("inline", "bounded"), required=True)
    parser.add_argument("--automations", type=int, default=8)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = make_url(args.database_url)
    require(
        url.drivername == "postgresql+psycopg"
        and not url.query
        and url.host == "127.0.0.1"
        and url.port == 60580
        and url.database == "sentinel_core_performance_test"
        and url.username == "sentinel_test",
        "Only the assigned disposable local PostgreSQL is allowed",
    )
    require(
        1 <= args.automations <= 16 and 1 <= args.requests <= 200 and 1 <= args.concurrency <= 8,
        "Bounded workload limits exceeded",
    )
    receipt = {
        "mode": args.mode,
        "capacity": 4,
        "automations": args.automations,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "seed": "sentinel-benchmark / fixed START",
        "scope": (
            "warmup accepts one HOLD per automation; "
            "measured requests replay immutable envelopes with identical current SQL"
        ),
        "warmup_requests": max(args.automations, args.concurrency),
        "warmup_concurrency": args.concurrency,
        "warmup_samples": [],
        "samples": [],
        "status": "running",
    }
    try:
        measure(args.database_url, args.mode, args.automations, args.requests, args.concurrency, receipt)
        receipt["status"] = "passed"
    except BaseException as error:
        receipt["status"] = "failed"
        receipt["exception_type"] = type(error).__name__
        raise
    finally:
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
