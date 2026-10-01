"""Repeat V0 full-drain parity on persisted datasets with a second seed."""

import argparse
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path
from shutil import copyfileobj

from develop.benchmarks.benchmark_fact_outbox_selector import dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("develop/reports/0.9.8-v0/second-seed"))
    parser.add_argument("--old-src", type=Path, default=Path("develop/reports/0.9.8-v0/old-head/src"))
    parser.add_argument("--seed", type=int, default=20260928)
    args = parser.parse_args()
    if not (args.old_src / "trading_automaton/storage/repository.py").is_file():
        parser.error("old production source snapshot is missing")
    args.output.mkdir(parents=True, exist_ok=True)
    cases = []
    for scenario in ("normal", "bootstrap", "many-bootstrap"):
        for count in (100, 10_000, 50_000):
            name = f"{scenario}-{count}-seed-{args.seed}"
            raw = args.output / f"{name}.sqlite"
            frozen = args.output / f"{name}.sqlite.gz"
            factory = dataset(raw, count, scenario=scenario, seed=args.seed)
            factory.kw["bind"].dispose()
            with raw.open("rb") as source, gzip.open(frozen, "wb", compresslevel=9) as target:
                copyfileobj(source, target)
            raw.unlink()
            results = {}
            for implementation in ("old-production", "current"):
                env = os.environ.copy()
                if implementation == "old-production":
                    env["PYTHONPATH"] = str(args.old_src.resolve())
                else:
                    env.pop("PYTHONPATH", None)
                command = [
                    sys.executable,
                    "-m",
                    "develop.benchmarks.fact_outbox_drain",
                    "--dataset",
                    str(frozen),
                    "--selector",
                    "current",
                    "--implementation-label",
                    implementation,
                ]
                if implementation == "current":
                    command.append("--count-read-rows")
                completed = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                result = json.loads(completed.stdout)
                (args.output / f"{name}-{implementation}.json").write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8"
                )
                results[implementation] = result
            keys = ("delivered_facts", "remaining_facts", "delivery_order_sha256")
            matching = all(results["old-production"][key] == results["current"][key] for key in keys)
            cases.append(
                {
                    "scenario": scenario,
                    "count": count,
                    "dataset": str(frozen),
                    "matches": matching,
                    "old": {key: results["old-production"][key] for key in keys},
                    "current": {key: results["current"][key] for key in keys},
                    "current_bootstrap_metadata_rows": results["current"]["bootstrap_read_rows"],
                }
            )
            if not matching:
                raise AssertionError(f"Full drain differs for {name}")
    summary = {"seed": args.seed, "batch_size": 1000, "cases": cases}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"seed": args.seed, "matching_cases": len(cases)}) + "\n")


if __name__ == "__main__":
    main()
