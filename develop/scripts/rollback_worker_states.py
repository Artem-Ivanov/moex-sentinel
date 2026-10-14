"""Explicit, offline rollback of Worker SQLite state CHECKs (revision 1 → 0)."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading_automaton.storage.database import create_worker_engine, downgrade_worker_states  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Existing SQLite file; stop the Worker before rollback")
    arguments = parser.parse_args()
    path = arguments.database.resolve(strict=True)
    engine = create_worker_engine(f"sqlite:///{path}")
    try:
        downgrade_worker_states(engine)
    finally:
        engine.dispose()
    print("Worker state CHECKs removed; financial rows and quota settings preserved.")  # noqa: T201


if __name__ == "__main__":
    main()
