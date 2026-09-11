"""Disposable A/B probe: switch only lookup implementation inside this process."""

# Standalone diagnostic script with progress output.
# ruff: noqa: INP001, T201

import asyncio
import importlib.util
import json
import os
import platform
from contextlib import contextmanager
from pathlib import Path

from develop.benchmarks.postgresql import run_case
from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository

ROOT = Path(__file__).resolve().parent


def baseline_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / "before" / "src" / "moex_sentinel" / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def lookup_version(version):
    if version == "after":
        yield
        return
    ingress = baseline_module("baseline_ingress", "services/trading_fact_ingress.py")
    audit = baseline_module("baseline_audit", "storage/repositories/trading_audit.py")
    original_publish = TradingFactIngressService._publish_group
    # Both _publish_group and its module-local rejection type must stay paired.
    original_public = TradingFactIngressService.publish
    TradingFactIngressService._publish_group = ingress.TradingFactIngressService._publish_group
    TradingFactIngressService.publish = ingress.TradingFactIngressService.publish
    names = ("get_envelope_by_event_id", "get_envelope_by_sequence")
    for name in names:
        if hasattr(TradingAuditRepository, name):
            raise RuntimeError("Baseline lookup is already installed in this probe process")
        setattr(TradingAuditRepository, name, getattr(audit.TradingAuditRepository, name))
    try:
        yield
    finally:
        TradingFactIngressService._publish_group = original_publish
        TradingFactIngressService.publish = original_public
        for name in names:
            delattr(TradingAuditRepository, name)


def main():
    results = []
    for version in ("before", "after", "after", "before"):
        with lookup_version(version):
            case = asyncio.run(
                run_case(os.environ["PG_PROFILE_DATABASE_URL"], 20, warmup=3, iterations=30, batch_size=100)
            )
        result = {"version": version, "case": case}
        results.append(result)
        # Persist each completed case so interrupted probes remain interpretable.
        output = {
            "python": platform.python_version(),
            "platform": platform.system(),
            "database_storage": "tmpfs",
            "purpose": "ABBA control on one disposable server; 30 WAIT ticks, not the 100-tick acceptance profile",
            "results": results,
        }
        (ROOT.parents[1] / "reports" / "postgresql-profile-envelope-abba-2026-09-10.json").write_text(
            json.dumps(output, indent=2) + "\n"
        )
        wait = case["wait"]
        print(
            json.dumps(
                {
                    "version": version,
                    "sql_per_decision": wait["sql_statement_count"] / wait["measured_decisions"],
                    "drain_p50_ms": wait["outbox_drain_ms"]["p50"],
                    "drain_p95_ms": wait["outbox_drain_ms"]["p95"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
