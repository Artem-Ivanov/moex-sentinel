"""Reproduce the bootstrap contract conflict from saved JSON, entirely offline.

Only the pure command projection and bootstrap validator are invoked. No service,
repository, database connection, or HTTP client is constructed. The validation
envelope is reconstructed from the saved pending-fact identity and the activation
payload defined by the inspected Worker code; it is not a captured HTTP payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from moex_sentinel.services.trading_fact_ingress import TradingFactIngressService, _FactGroupRejected
from moex_sentinel.storage.repositories.automation_commands import AutomationCommandRepository
from sentinel_contracts.trading_facts import AutomationStateChangedEnvelope


def reproduce(directory: Path) -> dict[str, object]:
    core_path, worker_path = directory / "core.json", directory / "worker.json"
    core = json.loads(core_path.read_text())
    worker = json.loads(worker_path.read_text())
    cached = {row["automation_id"]: row for row in worker["cached_automations"]}
    results = []
    for automation in core["automations"]:
        if not (
            automation["state"] == "IN_QUEUE"
            and automation["last_sequence_number"] == 0
            and automation["bootstrap_position_cycle_id"] is not None
        ):
            continue
        local = cached[automation["id"]]
        command = AutomationCommandRepository._command(
            SimpleNamespace(**automation),
            SimpleNamespace(id=automation["user_broker_id"], external_account_id=local["account_id"]),
            SimpleNamespace(
                id=local["fact_instrument_id"],
                external_instrument_id=local["instrument_id"],
                currency=local["currency"],
                lot_size=local["lot_size"],
                min_price_increment=local["min_price_increment"],
            ),
        )
        activations = [
            row
            for row in worker["pending_outbox"]
            if row["automation_id"] == automation["id"]
            and row["fact_kind"] == "AUTOMATION_STATE_CHANGED"
            and row["sequence_number"] == 1
        ]
        if len(activations) != 1:
            raise ValueError(f"Expected one saved initial activation for {automation['id']}")
        activation = activations[0]
        envelope = AutomationStateChangedEnvelope.model_validate(
            {
                "event_id": activation["event_id"],
                "user_broker_id": automation["user_broker_id"],
                "automation_id": automation["id"],
                "sequence_number": activation["sequence_number"],
                "expected_revision": automation["revision"],
                "fact_kind": activation["fact_kind"],
                "safe_message": activation["safe_message"],
                "occurred_at": datetime.fromisoformat(activation["occurred_at"] + "+00:00"),
                "payload": {
                    "state": "IN_WORK",
                    "suspended_from_state": None,
                    "hold_reason": None,
                    "closed_at": None,
                },
            }
        )
        rejection = None
        try:
            TradingFactIngressService._validate_initial_bootstrap(SimpleNamespace(**automation), [envelope])
        except _FactGroupRejected as error:
            rejection = error.code.value
        results.append(
            {
                "automation_id": automation["id"],
                "core_state": automation["state"],
                "core_bootstrap_position_cycle_id": automation["bootstrap_position_cycle_id"],
                "projected_command_bootstrap": (
                    None if command.bootstrap is None else command.bootstrap.model_dump(mode="json")
                ),
                "pending_activation_event_id": activation["event_id"],
                "pending_activation_sequence": activation["sequence_number"],
                "reconstructed_single_activation_validation": rejection or "ACCEPTED",
                "conflict_reproduced": command.bootstrap is None and rejection == "INVALID_FACT_STATE",
            }
        )
    if not results or not all(row["conflict_reproduced"] for row in results):
        raise RuntimeError("Saved input did not reproduce the expected bootstrap contract conflict")
    return {
        "mode": "OFFLINE_PURE_METHODS",
        "inputs": {
            path.name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in (core_path, worker_path)
        },
        "core_captured_at": core["captured_at"],
        "worker_captured_at": worker["captured_at"],
        "limitations": [
            "The two saved snapshots were captured at different times.",
            "The activation payload and expected revision are reconstructed from code and Core state.",
            "This demonstrates protocol incompatibility, not a captured Core rejection response "
            "or the original resume command.",
        ],
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("develop/reports/trading-audit-2026-09-12"))
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = reproduce(arguments.directory)
    output = arguments.output or arguments.directory / "bootstrap-reproduction.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), "reproduced": len(result["results"])}, ensure_ascii=False))  # noqa: T201


if __name__ == "__main__":
    main()
