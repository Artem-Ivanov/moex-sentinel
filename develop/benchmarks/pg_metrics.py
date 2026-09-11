"""Benchmark-only clocks; SQL/commit scopes deliberately overlap ingress/HTTP."""

from contextlib import contextmanager
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.orm import Session

from develop.benchmarks.runtime import distribution, require
from trading_automaton.storage.fact_outbox import FactOutboxWriter


class Profile:
    def __init__(self):
        self.samples = {}
        self.created = {}
        self.attempted = set()
        self.delivered_facts = 0
        self.active_ingress = False

    def add(self, name, elapsed):
        self.samples.setdefault(name, []).append(elapsed)

    @contextmanager
    def timing(self, name):
        started = perf_counter()
        try:
            yield
        finally:
            self.add(name, (perf_counter() - started) * 1000)

    def first_attempt(self, facts):
        attempted_at = perf_counter()
        for fact in facts:
            event_id = str(fact.event_id)
            if event_id not in self.attempted:
                require(event_id in self.created, "Missing monotonic outbox creation timestamp")
                self.add("queue_wait_ms", (attempted_at - self.created.pop(event_id)) * 1000)
                self.attempted.add(event_id)
                self.delivered_facts += 1

    def reset(self):
        require(not self.created, "Cannot change profile with unattempted queued facts")
        self.samples.clear()
        self.attempted.clear()
        self.delivered_facts = 0

    def report(self):
        result = {name: distribution(values) for name, values in self.samples.items() if values}
        result["sql_statement_count"] = len(self.samples.get("sql_execution_ms", []))
        result["delivered_facts"] = self.delivered_facts
        result["stage_totals_ms"] = {name: sum(values) for name, values in self.samples.items() if name.endswith("_ms")}
        return result


class TimedOutboxWriter(FactOutboxWriter):
    def __init__(self, profile, **kwargs):
        super().__init__(**kwargs)
        self.profile = profile

    def append(self, *args, **kwargs):
        envelope = super().append(*args, **kwargs)
        # append() has just added the real ORM outbox row; no domain timestamp
        # participates in latency. This includes its remaining transaction wait.
        self.profile.created[str(envelope.event_id)] = perf_counter()
        return envelope


def instrument_database(engine, profile):
    """Instrument only this disposable engine, including the DBAPI commit call."""
    original_commit = engine.dialect.do_commit

    def do_commit(connection):
        if not profile.active_ingress:
            return original_commit(connection)
        with profile.timing("dbapi_commit_ms"):
            return original_commit(connection)

    def before_cursor(_connection, _cursor, _statement, _parameters, context, _many):
        context.profile_started = perf_counter() if profile.active_ingress else None

    def after_cursor(_connection, _cursor, _statement, _parameters, context, _many):
        if context.profile_started is not None:
            profile.add("sql_execution_ms", (perf_counter() - context.profile_started) * 1000)

    engine.dialect.do_commit = do_commit
    event.listen(engine, "before_cursor_execute", before_cursor)
    event.listen(engine, "after_cursor_execute", after_cursor)

    class TimedSession(Session):
        def commit(self):
            if not profile.active_ingress:
                return super().commit()
            with profile.timing("session_commit_including_flush_ms"):
                return super().commit()

    def restore():
        engine.dialect.do_commit = original_commit
        event.remove(engine, "before_cursor_execute", before_cursor)
        event.remove(engine, "after_cursor_execute", after_cursor)

    return TimedSession, restore
