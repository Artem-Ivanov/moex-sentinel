# D1 worker: post-minor final delta vs original baseline
Only changes since accepted feature freeze: tests/api/test_runtime_versions.py

## tests/api/test_runtime_versions.py

```diff
--- baseline/tests/api/test_runtime_versions.py
+++ tests/api/test_runtime_versions.py
@@ -8,9 +8,15 @@
 from fastapi.testclient import TestClient
 
 from moex_sentinel.api.app import create_app
+from moex_sentinel.api.worker_auth import WorkerPrincipal
 from moex_sentinel.services.runtime_versions import WorkerVersionStore
 from sentinel_contracts.runtime_versions import RuntimeVersion
-from sentinel_contracts.worker_diagnostics import CommandSyncDiagnostics, CommandSyncRow
+from sentinel_contracts.worker_diagnostics import (
+    CommandSyncDiagnostics,
+    CommandSyncRow,
+    WorkerAutomationExecutionDiagnostics,
+    WorkerExecutionDiagnostics,
+)
 from tests.services.test_runtime_versions import progress
 from trading_automaton.adapters.core_client import CoreClient
 
@@ -172,6 +178,257 @@
     assert worker["broker_runtime"]["status"] == "UNKNOWN"
     assert worker["broker_runtime"]["decision_eligibility"] == "UNKNOWN"
     assert worker["command_sync"] is None
+
+
+def test_invalid_optional_execution_projection_does_not_reject_control_heartbeat(tmp_path):
+    now = datetime.now(UTC)
+    payload = {
+        "worker_id": "worker",
+        "occurred_at": now.isoformat(),
+        "runtime_version": {
+            "version": "1.2.3",
+            "instance_id": str(uuid4()),
+            "environment": "TEST",
+            "access_mode": "READ_ONLY",
+        },
+        "worker_diagnostics": progress(completed=now, finished=now).model_dump(mode="json"),
+    }
+    payload["worker_diagnostics"]["execution_diagnostics"] = {
+        "observation": "OBSERVED",
+        "reason": "OBSERVED",
+        "broker_id": str(uuid4()),
+        "captured_at": now.isoformat(),
+        "rows": [{"account_id": "secret", "raw_payload": "must-not-be-stored"}],
+    }
+
+    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'invalid-execution.db'}")
+    with TestClient(application) as client:
+        response = client.post("/internal/automaton/heartbeats", json=payload)
+        status = client.get("/api/diagnostics/status").json()
+
+    assert response.status_code == 200
+    assert status["worker"]["reason"] == "CONTROL_PROGRESS"
+    assert "execution_diagnostics" not in status["worker"]
+    assert "must-not-be-stored" not in response.text + str(status)
+    assert client.app.state.worker_versions._execution_projection.reason == "INVALID_PROJECTION"
+
+
+def test_unauthenticated_test_execution_projection_is_not_publicly_exposed(tmp_path):
+    now = datetime.now(UTC)
+    broker_id, automation_id, fact_instrument_id = uuid4(), uuid4(), uuid4()
+    diagnostics = progress(completed=now, finished=now).model_copy(
+        update={
+            "execution_diagnostics": WorkerExecutionDiagnostics(
+                observation="OBSERVED",
+                reason="OBSERVED",
+                broker_id=broker_id,
+                captured_at=now,
+                rows=(
+                    WorkerAutomationExecutionDiagnostics(
+                        automation_id=automation_id,
+                        account_id="test-account",
+                        external_instrument_id="test-instrument",
+                        fact_instrument_id=fact_instrument_id,
+                        lot_size=1,
+                        pending_fact_count=2,
+                        failed_fact_count=0,
+                    ),
+                ),
+            )
+        }
+    )
+    payload = {
+        "worker_id": "worker",
+        "occurred_at": now.isoformat(),
+        "runtime_version": {
+            "version": "1.2.3",
+            "instance_id": str(uuid4()),
+            "environment": "TEST",
+            "access_mode": "READ_ONLY",
+        },
+        "worker_diagnostics": diagnostics.model_dump(mode="json"),
+    }
+    application = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'test-execution.db'}")
+    with TestClient(application) as client:
+        assert client.post("/internal/automaton/heartbeats", json=payload).status_code == 200
+        status = client.get("/api/diagnostics/status").json()
+        stored_execution = client.app.state.worker_versions._execution_projection
+        scoped = client.app.state.worker_versions.execution_snapshot(
+            broker_id=broker_id,
+            automation_id=automation_id,
+            account_id="test-account",
+            instrument_id="test-instrument",
+            fact_instrument_id=fact_instrument_id,
+            lot_size=1,
+            owner_runtime_instance_id=None,
+            owner_generation=None,
+        )
+
+    assert status["worker"]["reason"] == "CONTROL_PROGRESS"
+    assert "execution_diagnostics" not in status["worker"]
+    assert stored_execution.observation == "UNKNOWN"
+    assert stored_execution.reason == "UNAUTHENTICATED_SOURCE"
+    assert stored_execution.rows == ()
+    assert scoped.reason == "UNAUTHENTICATED_SOURCE"
+
+
+@pytest.mark.parametrize("mismatch", [("instance_id",), ("environment",), ("access_mode",), ("broker_id",)])
+def test_authenticated_prod_heartbeat_rejects_diagnostic_scope_mismatch_without_observing(
+    tmp_path, monkeypatch, mismatch
+):
+    (mismatch,) = mismatch
+    monkeypatch.setenv("APPLICATION_ENVIRONMENT", "PROD")
+    monkeypatch.setenv("BROKER_ACCESS_MODE", "READ_ONLY")
+    monkeypatch.setenv("AUTH_ALLOWED_ORIGIN", "https://operator.example.test")
+    monkeypatch.setenv("AUTH_SESSION_COOKIE_NAME", "__Host-prod-worker-test")
+    monkeypatch.setenv("INTERNAL_WORKER_ID", "worker-prod")
+    broker_id, other_broker_id = uuid4(), uuid4()
+    instance_id, other_instance_id = uuid4(), uuid4()
+    monkeypatch.setenv("INTERNAL_WORKER_BROKER_ID", str(broker_id))
+    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", "a" * 64)
+
+    class PrincipalAuthenticator:
+        def authenticate(self, **_kwargs):
+            return WorkerPrincipal(
+                worker_id="worker-prod",
+                runtime_instance_id=instance_id,
+                broker_id=broker_id,
+                environment="PROD",
+                access_mode="READ_ONLY",
+                strategy_enabled=False,
+                generation=3,
+                registration_status="OWNER",
+                recovery_only=False,
+            )
+
+    now = datetime.now(UTC)
+    automation_id, fact_instrument_id = uuid4(), uuid4()
+    projection = WorkerExecutionDiagnostics(
+        observation="OBSERVED",
+        reason="OBSERVED",
+        broker_id=broker_id,
+        captured_at=now,
+        rows=(
+            WorkerAutomationExecutionDiagnostics(
+                automation_id=automation_id,
+                account_id="account-prod",
+                external_instrument_id="external-prod",
+                fact_instrument_id=fact_instrument_id,
+                lot_size=10,
+                pending_fact_count=0,
+                failed_fact_count=0,
+            ),
+        ),
+    )
+    diagnostics = progress(completed=now, finished=now).model_copy(update={"execution_diagnostics": projection})
+    base = {
+        "worker_id": "worker-prod",
+        "occurred_at": now.isoformat(),
+        "runtime_version": {
+            "version": "6.6.6",
+            "instance_id": str(instance_id),
+            "environment": "PROD",
+            "access_mode": "READ_ONLY",
+        },
+        "worker_diagnostics": diagnostics.model_dump(mode="json"),
+    }
+    request = base.copy()
+    request["occurred_at"] = (now.replace(microsecond=0) + timedelta(seconds=2)).isoformat()
+    if mismatch != "broker_id":
+        runtime_changes = {
+            "instance_id": {"instance_id": str(other_instance_id)},
+            "environment": {"environment": "TEST"},
+            "access_mode": {"access_mode": "TRADE"},
+        }
+        request["runtime_version"] = {**base["runtime_version"], **runtime_changes[mismatch]}
+        request["runtime_version"]["version"] = "9.9.9"
+    else:
+        changed_diagnostics = diagnostics.model_copy(
+            update={"execution_diagnostics": projection.model_copy(update={"broker_id": other_broker_id})}
+        )
+        request["worker_diagnostics"] = changed_diagnostics.model_dump(mode="json")
+
+    app = create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / f'prod-scope-{mismatch}.db'}")
+    with TestClient(app) as client:
+        client.app.state.worker_authenticator = PrincipalAuthenticator()
+        headers = {
+            "Authorization": f"Bearer {'a' * 64}",
+            "X-Runtime-Instance-ID": str(instance_id),
+            "X-Runtime-Secret": "b" * 64,
+        }
+        matched = client.post("/internal/automaton/heartbeats", json=base, headers=headers)
+        store = client.app.state.worker_versions
+        cached_before = (
+            store._version,
+            store._occurred_at,
+            store._diagnostics,
+            store._execution_projection,
+            store._execution_runtime_instance_id,
+            store._execution_generation,
+            store._execution_broker_id,
+        )
+        rejected = client.post("/internal/automaton/heartbeats", json=request, headers=headers)
+        cached_after = (
+            store._version,
+            store._occurred_at,
+            store._diagnostics,
+            store._execution_projection,
+            store._execution_runtime_instance_id,
+            store._execution_generation,
+            store._execution_broker_id,
+        )
+
+    assert matched.status_code == 200
+    assert rejected.status_code == 403
+    assert cached_after == cached_before
+
+
+@pytest.mark.parametrize(
+    "execution_projection",
+    [
+        None,
+        {
+            "observation": "OBSERVED",
+            "reason": "OBSERVED",
+            "broker_id": str(uuid4()),
+            "captured_at": datetime.now(UTC).isoformat(),
+            "rows": [{}] * 101,
+        },
+        {
+            "observation": "OBSERVED",
+            "reason": "OBSERVED",
+            "broker_id": str(uuid4()),
+            "captured_at": datetime.now(UTC).isoformat(),
+            "rows": [{"intents": [{}] * 6}],
+        },
+    ],
+)
+def test_optional_execution_projection_null_and_oversize_prevalidation_preserve_control(tmp_path, execution_projection):
+    now = datetime.now(UTC)
+    diagnostics = progress(completed=now, finished=now).model_dump(mode="json")
+    diagnostics["execution_diagnostics"] = execution_projection
+    payload = {
+        "worker_id": "worker",
+        "occurred_at": now.isoformat(),
+        "runtime_version": {
+            "version": "1.2.3",
+            "instance_id": str(uuid4()),
+            "environment": "TEST",
+            "access_mode": "READ_ONLY",
+        },
+        "worker_diagnostics": diagnostics,
+    }
+    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path/'prevalidate.db'}")) as client:
+        response = client.post("/internal/automaton/heartbeats", json=payload)
+        stored = client.app.state.worker_versions._execution_projection
+
+    assert response.status_code == 200
+    assert response.json()["worker_id"] == "worker"
+    if execution_projection is None:
+        assert stored is None
+    else:
+        assert stored.reason == "INVALID_PROJECTION"
+        assert stored.rows == ()
 
 
 def test_worker_command_sync_projection_flows_through_existing_heartbeat(tmp_path):
```
