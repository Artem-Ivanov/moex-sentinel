"""Real loopback HTTP boundary with one deliberately truncated committed ACK."""

import socket
import threading
from time import monotonic, sleep
from types import SimpleNamespace

import httpx
import uvicorn
from fastapi import FastAPI

from develop.benchmarks.runtime import require
from moex_sentinel.usecases.trading_fact_ingress import PublishTradingFactsUsecase
from moex_sentinel.views.internal_trading_facts import router
from trading_automaton.adapters.core_client import CoreClient


class TimedIngress:
    def __init__(self, ingress, profile):
        self.ingress = ingress
        self.profile = profile

    def publish(self, facts):
        self.profile.active_ingress = True
        sql_before = len(self.profile.samples.get("sql_execution_ms", []))
        commits_before = len(self.profile.samples.get("dbapi_commit_ms", []))
        try:
            with self.profile.timing("core_ingress_ms"):
                result = self.ingress.publish(facts)
            require(not result.failures, "Production Core ingress rejected synthetic facts")
            require(
                {item for group in result.results for item in group.accepted_event_ids}
                == {fact.event_id for fact in facts},
                "Core did not acknowledge every requested event",
            )
            return result
        finally:
            self.profile.add(
                "sql_statements_per_request", len(self.profile.samples.get("sql_execution_ms", [])) - sql_before
            )
            self.profile.add(
                "dbapi_commits_per_request", len(self.profile.samples.get("dbapi_commit_ms", [])) - commits_before
            )
            self.profile.add("facts_per_request", len(facts))
            self.profile.active_ingress = False


class LoseAcknowledgement:
    def __init__(self, app):
        self.app = app
        self.armed = False

    async def __call__(self, scope, receive, send):
        if not self.armed or scope["type"] != "http" or scope["path"] != "/internal/automation-facts":
            await self.app(scope, receive, send)
            return
        messages = []

        async def capture(message):
            messages.append(message)

        await self.app(scope, receive, capture)
        require(messages[0]["status"] == 200, "Lost ACK injection requires a successful committed response")
        self.armed = False
        # Uvicorn closes the connection when the app returns an incomplete body.
        # The actual successful router/ingress response never reaches CoreClient.
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"1")]})
        await send({"type": "http.response.body", "body": b"", "more_body": True})


class CoreServer:
    def __init__(self, ingress, profile):
        app = FastAPI()
        app.state.usecases = SimpleNamespace(
            publish_trading_facts=PublishTradingFactsUsecase(TimedIngress(ingress, profile))
        )
        app.include_router(router)
        self.app = LoseAcknowledgement(app)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, lifespan="off", access_log=False, log_level="critical", loop="asyncio")
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    def start(self):
        self.thread.start()
        deadline = monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or monotonic() > deadline:
                self.close()
                raise RuntimeError("Disposable Core HTTP server did not start")
            sleep(0.005)

    def close(self):
        self.server.should_exit = True
        if self.thread.ident is not None:
            self.thread.join(timeout=10)
        self.socket.close()
        require(not self.thread.is_alive(), "Disposable Core HTTP server did not stop")


class NetworkDelivery:
    def __init__(self, url, profile):
        self.profile = profile
        self.http = httpx.Client(base_url=url, timeout=30, trust_env=False)
        self.client = CoreClient(self.http)
        self.replay_expected = {}
        self.replayed_facts = 0
        self.transport_errors = 0

    def publish_facts(self, facts):
        self.profile.first_attempt(facts)
        try:
            with self.profile.timing("http_roundtrip_ms"):
                result = self.client.publish_facts(facts)
        except httpx.TransportError:
            self.transport_errors += 1
            self.replay_expected = {str(fact.event_id): fact.model_dump(mode="json") for fact in facts}
            raise
        require(not result.failures, "Core rejected synthetic facts over HTTP")
        for fact in facts:
            original = self.replay_expected.pop(str(fact.event_id), None)
            if original is not None:
                require(original == fact.model_dump(mode="json"), "Restart altered an immutable envelope")
                self.replayed_facts += 1
        return result

    def close(self):
        self.http.close()
