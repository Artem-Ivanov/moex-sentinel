"""Validate and compare the fixed synthetic acceptance profiles for milestone 0.9.7."""

# Standalone acceptance assertions and machine-readable CLI output.
# ruff: noqa: INP001, S101, T201

import json
from pathlib import Path

REPORTS = Path(__file__).resolve().parents[2] / "reports"
BEFORE = "postgresql-profile-cas-2026-09-09.json"
AFTER = "postgresql-profile-envelope-2026-09-10.json"
OUTPUT = "postgresql-profile-envelope-comparison-2026-09-10.json"


def main() -> None:
    if not __debug__:
        raise RuntimeError("Run profile validation without Python optimization so assertions remain active")
    before = json.loads((REPORTS / BEFORE).read_text())
    after = json.loads((REPORTS / AFTER).read_text())
    for key in before.keys() - {"results"}:
        assert before[key] == after[key], key
    assert [case["instruments"] for case in before["results"]] == [6, 20, 50]
    assert [case["instruments"] for case in after["results"]] == [6, 20, 50]
    results = []
    for old, new in zip(before["results"], after["results"], strict=True):
        for key in (
            "instruments",
            "postgresql_version",
            "warmup",
            "iterations",
            "batch_size",
            "deadline_ms",
            "retry_limit",
        ):
            assert old[key] == new[key], key
        stages = {}
        for stage in ("initial_buy_filled", "wait"):
            baseline, current = old[stage], new[stage]
            for key in ("delivered_facts", "measured_decisions"):
                assert baseline[key] == current[key], key
            assert baseline["sql_statement_count"] - current["sql_statement_count"] == current["delivered_facts"]
            metrics = {}
            for key, value in current.items():
                if not isinstance(value, dict) or "samples" not in value:
                    continue
                assert value["min"] <= value["p50"] <= value["p95"] <= value["p99"] <= value["max"], key
                if key != "sql_execution_ms":
                    assert baseline[key]["samples"] == value["samples"], key
                metrics[key] = {
                    "before": baseline[key],
                    "after": value,
                    "p95_change_percent": (value["p95"] / baseline[key]["p95"] - 1) * 100,
                }
            assert current["sql_execution_ms"]["samples"] == current["sql_statement_count"]
            stages[stage] = {
                "decisions": current["measured_decisions"],
                "facts": current["delivered_facts"],
                "sql_before": baseline["sql_statement_count"],
                "sql_after": current["sql_statement_count"],
                "sql_per_decision_before": baseline["sql_statement_count"] / baseline["measured_decisions"],
                "sql_per_decision_after": current["sql_statement_count"] / current["measured_decisions"],
                "pipeline_decisions_per_second_before": baseline["pipeline_decisions_per_second"],
                "pipeline_decisions_per_second_after": current["pipeline_decisions_per_second"],
                "metrics": metrics,
            }
        assert (
            old["wait"]["reason_counts"] == new["wait"]["reason_counts"] == {"NO_THRESHOLD": new["instruments"] * 100}
        )
        assert (
            old["initial_buy_filled"]["broker_fills"] == new["initial_buy_filled"]["broker_fills"] == new["instruments"]
        )
        recovery = new["recovery"]
        assert recovery["backlog_before_restart"] == recovery["backlog_after_restart"] > 0
        assert recovery["backlog_final"] == 0
        assert recovery["committed_before_restart"] == recovery["replayed_facts"] > 0
        assert recovery["lost_ack_transport_errors"] == 1
        assert recovery["worker_decisions"] == recovery["core_decisions"]
        assert recovery["sdk_dispatches_before"] == recovery["sdk_dispatches_after"] == new["instruments"]
        assert recovery["core_orders_before"] == recovery["core_orders_after"] == new["instruments"]
        assert recovery["immutable_envelopes_preserved"] is True
        results.append({"instruments": new["instruments"], "stages": stages, "recovery": recovery})
    (REPORTS / OUTPUT).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "before": BEFORE,
                "after": AFTER,
                "matching_conditions_verified": True,
                "limitations": "Separate runs on a shared machine; tmpfs; serial synthetic workload; no SLA threshold.",
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    for result in results:
        wait = result["stages"]["wait"]
        print(
            json.dumps(
                {
                    "instruments": result["instruments"],
                    "sql_before": wait["sql_per_decision_before"],
                    "sql_after": wait["sql_per_decision_after"],
                    "drain_p95_change_percent": wait["metrics"]["outbox_drain_ms"]["p95_change_percent"],
                }
            )
        )


if __name__ == "__main__":
    main()
