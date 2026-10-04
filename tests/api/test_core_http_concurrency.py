"""Real HTTP requests keep the loop responsive while sync database work waits."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from moex_sentinel.api import app as app_module
from moex_sentinel.api.sync_execution import SyncExecutor
from moex_sentinel.storage.repositories import RecordNotFoundError
from moex_sentinel.usecases.automations import CreateTradingAutomationUsecase, ViewTradingAutomationDetailsUsecase
from moex_sentinel.usecases.errors import UseCaseError
from sentinel_contracts.audit import current_process_id


class QuietGateway:
    async def close(self):
        pass


def test_blocked_database_request_does_not_block_stateless_http_and_keeps_session_in_one_worker(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    observed = []
    process_id = "9c8a54fd-0000-4000-8000-000000000001"

    def build(factory, *, settings):
        def claim(_limit):
            with factory() as session:
                observed.append(("create", threading.get_ident(), current_process_id()))
                session.scalar(text("SELECT 1"))
                observed.append(("use", threading.get_ident(), current_process_id()))
                entered.set()
                assert release.wait(3), "Test watchdog failed to release the database operation"
            observed.append(("close", threading.get_ident(), current_process_id()))
            return ()

        return SimpleNamespace(claim_automation_commands=SimpleNamespace(execute=claim))

    monkeypatch.setattr(app_module, "build_application_usecases", build)
    monkeypatch.setattr(app_module, "build_market_snapshot_gateway", lambda *_args, **_kwargs: QuietGateway())
    application = app_module.create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'http.db'}")

    def watchdog():
        if entered.wait(3):
            release.wait(1)
        release.set()

    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()

    async def scenario():
        loop_thread = threading.get_ident()
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client,
        ):
            database = asyncio.create_task(
                client.post("/internal/automation-commands", json={"limit": 1}, headers={"X-Process-ID": process_id})
            )
            try:
                while not entered.is_set():  # noqa: ASYNC110 - cross-thread test barrier
                    await asyncio.sleep(0.001)
                runtime = await client.get("/internal/runtime")
                responsive = not release.is_set()
            finally:
                release.set()
                response = await database
            assert runtime.status_code == response.status_code == 200
            assert responsive, "Stateless HTTP waited for a blocked sync database request on the event loop"
            assert [event[0] for event in observed] == ["create", "use", "close"]
            assert len({event[1] for event in observed}) == 1
            assert observed[0][1] != loop_thread
            assert {event[2] for event in observed} == {process_id}

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog_thread.join(timeout=3)


def test_lifespan_repeated_raw_cancellation_drains_actual_jobs_before_engine_disposal(tmp_path, monkeypatch):
    entered, release, finished, disposed = (threading.Event() for _ in range(4))
    original_engine = app_module.create_database_engine

    def engine_factory(url):
        engine = original_engine(url)
        original_dispose = engine.dispose

        def dispose():
            assert finished.is_set(), "Engine disposed before its unfinished synchronous job"
            disposed.set()
            original_dispose()

        engine.dispose = dispose
        return engine

    monkeypatch.setattr(app_module, "create_database_engine", engine_factory)
    monkeypatch.setattr(app_module, "build_application_usecases", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(app_module, "build_market_snapshot_gateway", lambda *_args, **_kwargs: QuietGateway())
    application = app_module.create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'cancel.db'}")

    def blocked():
        entered.set()
        assert release.wait(3)
        finished.set()

    async def scenario():
        ready = asyncio.Event()
        job = None

        async def owner():
            nonlocal job
            async with application.router.lifespan_context(application):
                job = asyncio.create_task(application.state.sync_executor.run(blocked))
                ready.set()
                await asyncio.Event().wait()

        owner_task = asyncio.create_task(owner())
        await ready.wait()
        while not entered.is_set():  # noqa: ASYNC110 - cross-thread test barrier
            await asyncio.sleep(0.001)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        owner_task.cancel()
        await asyncio.sleep(0)
        owner_task.cancel()
        await asyncio.sleep(0)
        assert not owner_task.done()
        assert not disposed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner_task
        assert disposed.is_set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize("stage", ["pool", "build", "gateway", "analytics"])
def test_startup_failure_closes_created_pool_gateway_and_engine(tmp_path, monkeypatch, stage):

    original_engine = app_module.create_database_engine
    pools, closed = [], []

    def engine_factory(url):
        engine = original_engine(url)
        original_dispose = engine.dispose

        def dispose():
            closed.append("engine")
            original_dispose()

        engine.dispose = dispose
        return engine

    def pool_factory(**kwargs):
        if stage == "pool":
            raise ValueError("synthetic startup failure")
        pool = SyncExecutor(**kwargs)
        pools.append(pool)
        return pool

    def build(*_args, **_kwargs):
        if stage == "build":
            raise ValueError("synthetic startup failure")
        return SimpleNamespace()

    class Gateway:
        async def close(self):
            closed.append("gateway")

    def gateway(*_args, **_kwargs):
        if stage == "gateway":
            raise ValueError("synthetic startup failure")
        return Gateway()

    monkeypatch.setattr(app_module, "create_database_engine", engine_factory)
    monkeypatch.setattr(app_module, "SyncExecutor", pool_factory)
    monkeypatch.setattr(app_module, "build_application_usecases", build)
    monkeypatch.setattr(app_module, "build_market_snapshot_gateway", gateway)
    if stage == "analytics":

        def failed_client(**_kwargs):
            raise ValueError("synthetic startup failure")

        monkeypatch.setattr(app_module.httpx, "AsyncClient", failed_client)
    application = app_module.create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'startup.db'}")

    async def scenario():
        with pytest.raises(ValueError, match="synthetic startup failure"):
            async with application.router.lifespan_context(application):
                pytest.fail("Failed startup must not yield")
        assert closed == (["gateway", "engine"] if stage == "analytics" else ["engine"])
        for pool in pools:
            with pytest.raises(RuntimeError, match="closed"):
                await pool.run(lambda: "late")

    asyncio.run(scenario())


def test_named_sync_http_routes_and_async_create_details_use_request_worker_context(tmp_path, monkeypatch):
    observed = []
    process_id = "9c8a54fd-0000-4000-8000-000000000002"

    def usecase(name):
        def execute(*_args, **_kwargs):
            observed.append((name, threading.get_ident(), current_process_id()))
            raise UseCaseError("AUTOMATION_NOT_FOUND", "Synthetic scope failure")

        return SimpleNamespace(execute=execute)

    names = (
        "claim_automation_commands",
        "view_automation_statuses",
        "publish_trading_facts",
        "view_trading_automations",
        "view_trading_automation",
        "hold_automation",
        "resume_automation",
        "close_automation",
        "view_automaton_broker_connection",
        "view_automaton_broker_scope",
        "view_broker_settings",
        "save_broker_settings",
        "delete_broker_settings",
        "set_instrument_selection",
        "view_trading_summary",
    )

    class Automations:
        def create(self, **_kwargs):
            observed.append(("create", threading.get_ident(), current_process_id()))
            raise RecordNotFoundError("Synthetic missing scope")

        def get(self, _automation_id):
            observed.append(("details_initial", threading.get_ident(), current_process_id()))
            raise RecordNotFoundError("Synthetic missing scope")

    values = {name: usecase(name) for name in names}
    values["create_trading_automation"] = CreateTradingAutomationUsecase(Automations())
    values["view_trading_automation_details"] = ViewTradingAutomationDetailsUsecase(Automations(), None, None)
    monkeypatch.setattr(app_module, "build_application_usecases", lambda *_args, **_kwargs: SimpleNamespace(**values))
    monkeypatch.setattr(app_module, "build_market_snapshot_gateway", lambda *_args, **_kwargs: QuietGateway())
    application = app_module.create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'named.db'}")
    draft = {
        "display_name": "Synthetic",
        "provider_code": "TINVEST",
        "environment_code": "SANDBOX",
        "adapter_code": "TINVEST_SANDBOX",
        "enabled": True,
        "fields": [],
        "is_test": True,
    }
    cases = [
        ("POST", "/internal/automation-commands", {"limit": 1}, "claim_automation_commands"),
        ("POST", "/internal/automation-statuses", {"automation_ids": []}, "view_automation_statuses"),
        ("POST", "/internal/automation-facts", {"facts": []}, "publish_trading_facts"),
        ("GET", "/api/trading-automations", None, "view_trading_automations"),
        ("GET", "/api/trading-automations/a", None, "view_trading_automation"),
        *[
            ("POST", f"/api/trading-automations/a/{action}", None, f"{action}_automation")
            for action in ("hold", "resume", "close")
        ],
        ("GET", "/internal/automaton/brokers/b/connection", None, "view_automaton_broker_connection"),
        ("GET", "/internal/automaton/brokers/b/scope", None, "view_automaton_broker_scope"),
        ("GET", "/api/brokers", None, "view_broker_settings"),
        ("POST", "/api/brokers", draft, "save_broker_settings"),
        ("PUT", "/api/brokers/b", draft, "save_broker_settings"),
        ("DELETE", "/api/brokers/b", None, "delete_broker_settings"),
        ("PUT", "/api/brokers/b/instruments/i/selection", {"selected": True}, "set_instrument_selection"),
        ("GET", "/api/trading/summary", None, "view_trading_summary"),
        ("GET", "/api/health", None, "health"),
        ("GET", "/api/diagnostics/status", None, "diagnostics"),
        ("POST", "/api/instruments/b/i/trade", {"account_id": "synthetic"}, "create"),
        ("GET", "/api/trading-automations/a/details", None, "details_initial"),
    ]

    async def scenario():
        loop_thread = threading.get_ident()
        async with application.router.lifespan_context(application):
            application.state.readiness_usecase = usecase("health")
            application.state.diagnostics_usecase = usecase("diagnostics")

            async def observe():
                assert threading.get_ident() == loop_thread

            application.state.analytics_versions = SimpleNamespace(observe=observe)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                for method, path, payload, name in cases:
                    response = await client.request(method, path, json=payload, headers={"X-Process-ID": process_id})
                    assert response.status_code == 404, (path, response.text)
                    assert response.json()["detail"]["code"] == "AUTOMATION_NOT_FOUND"
                    assert observed[-1][0] == name
                    assert observed[-1][1] != loop_thread
                    assert observed[-1][2] == process_id
        assert len(observed) == len(cases)

    asyncio.run(scenario())


def test_overlapping_http_jobs_keep_distinct_business_process_contexts(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    observed = {}
    process_ids = ["9c8a54fd-0000-4000-8000-000000000003", "9c8a54fd-0000-4000-8000-000000000004"]

    def build(factory, *, settings):
        def claim(limit):
            with factory() as session:
                session.scalar(text("SELECT 1"))
                barrier.wait(timeout=3)
                observed[limit] = (current_process_id(), threading.get_ident())
            return ()

        return SimpleNamespace(claim_automation_commands=SimpleNamespace(execute=claim))

    monkeypatch.setattr(app_module, "build_application_usecases", build)
    monkeypatch.setattr(app_module, "build_market_snapshot_gateway", lambda *_args, **_kwargs: QuietGateway())
    application = app_module.create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'contexts.db'}")

    async def scenario():
        loop_thread = threading.get_ident()
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client,
        ):
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/internal/automation-commands", json={"limit": index + 1}, headers={"X-Process-ID": process_id}
                    )
                    for index, process_id in enumerate(process_ids)
                )
            )
            assert [response.status_code for response in responses] == [200, 200]
            assert [response.headers["X-Process-ID"] for response in responses] == process_ids
            assert [observed[index + 1][0] for index in range(2)] == process_ids
            assert all(thread != loop_thread for _process_id, thread in observed.values())
        assert current_process_id() is None

    asyncio.run(scenario())
