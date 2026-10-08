#!/usr/bin/env python3
"""Dependency-free smoke check for the portable Codex hook template."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent
HOOKS = TEMPLATE / ".codex" / "hooks"
SKILL = TEMPLATE / ".agents" / "skills" / "planning-with-files"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_sources() -> tuple[dict, list[str]]:
    manifest = json.loads((TEMPLATE / ".codex" / "hooks.json").read_text())
    events = manifest.get("hooks", {})
    expected = {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
        "PostToolUse", "PreCompact", "Stop",
    }
    require(set(events) == expected, f"hook events differ: {sorted(events)}")
    config = tomllib.loads((TEMPLATE / ".codex" / "config.toml").read_text())
    agents = config.get("agents", {})
    require(agents.get("enabled") is True and agents.get("max_concurrent_threads_per_session") == 3,
            "unexpected [agents] settings")
    roles = {name: value for name, value in agents.items() if isinstance(value, dict)}
    require(set(roles) == {
        "architect", "backend_developer", "frontend_developer", "junior_backend_developer",
        "junior_frontend_developer", "reviewer", "security_auditor", "worker",
    }, f"unexpected role registrations: {sorted(roles)}")
    for name, role in roles.items():
        profile = (TEMPLATE / ".codex" / role.get("config_file", "")).resolve()
        require(profile.is_relative_to(TEMPLATE.resolve()) and profile.is_file(),
                f"{name} profile path is missing or escapes template: {role.get('config_file')}")
        profile_data = tomllib.loads(profile.read_text())
        require(profile_data.get("name") == name and isinstance(profile_data.get("model"), str)
                and profile_data["model"] and isinstance(profile_data.get("developer_instructions"), str)
                and profile_data["developer_instructions"].strip(),
                f"{name} profile must define matching name, model, and instructions")
    require((SKILL / "scripts" / "resolve-plan-dir.sh").is_file(),
            "missing resolver dependency at canonical .agents/skills path")
    require((SKILL / "scripts" / "check-complete.sh").is_file(),
            "missing stop-hook check-complete dependency")
    require((SKILL / "scripts" / "session-catchup.py").is_file(),
            "missing session-start catchup dependency")

    shell_files = sorted(HOOKS.glob("*.sh")) + sorted((SKILL / "scripts").glob("*.sh"))
    for path in shell_files:
        result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
        require(result.returncode == 0, f"shell syntax error in {path}: {result.stderr}")
    python_files = sorted(HOOKS.glob("*.py")) + sorted((SKILL / "scripts").glob("*.py"))
    for path in python_files:
        compile(path.read_bytes(), str(path), "exec")  # no bytecode artifacts
    return events, ["JSON/TOML", f"{len(shell_files)} shell files", f"{len(python_files)} Python files"]


def state_key(project: Path, session_id: str) -> str:
    digest = hashlib.sha256()
    for value in ("codex", str(project.resolve()), session_id):
        encoded = value.encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def run_event(event: str, config: dict, root: Path, payload: str, env: dict[str, str]) -> str:
    entries = config[event]
    require(len(entries) == 1 and len(entries[0]["hooks"]) == 1,
            f"unexpected command layout for {event}")
    command = entries[0]["hooks"][0]["command"]
    result = subprocess.run(command, shell=True, cwd=root, input=payload, text=True,
                            capture_output=True, env=env, timeout=20)
    require(result.returncode == 0, f"{event} failed ({result.returncode}): {result.stderr}")
    return result.stdout.strip()


def decode_output(raw: str, event: str) -> str:
    require(bool(raw), f"{event} emitted no expected context")
    value = json.loads(raw)
    specific = value.get("hookSpecificOutput", {})
    return "\n".join(str(value.get(key, "")) for key in ("systemMessage", "followup_message")) + "\n" + str(specific.get("additionalContext", ""))


def main() -> None:
    events, checks = check_sources()
    with tempfile.TemporaryDirectory(prefix="codex template smoke ", dir=HERE) as scratch:
        sandbox = Path(scratch) / "workspace with spaces"
        shutil.copytree(TEMPLATE / ".codex", sandbox / ".codex")
        shutil.copytree(TEMPLATE / ".agents", sandbox / ".agents")
        (sandbox / ".planning" / "sessions").mkdir(parents=True)
        plan_dir = sandbox / ".planning" / "smoke"
        plan_dir.mkdir()
        (plan_dir / "task_plan.md").write_text(
            "# Smoke plan\n\n### Phase 1: Check\n- **Status:** in_progress\n", encoding="utf-8")
        (plan_dir / "progress.md").write_text("Smoke progress marker\n", encoding="utf-8")
        sid = "smoke-session"
        (sandbox / ".planning" / "sessions" / f"{state_key(sandbox, sid)}.attached").touch()
        home = Path(scratch) / "isolated home"
        home.mkdir()
        env = os.environ.copy()
        for key in ("PWF_PLAN_ROOT", "PLANNING_DISABLED", "PWF_HOOK_MODE", "PWF_GOAL_CHECK",
                    "PWF_TRUSTED_PYTHON", "PLAN_ID"):
            env.pop(key, None)
        env.update({"HOME": str(home), "XDG_CACHE_HOME": str(home / "cache"),
                    "PWF_SESSION_ID": sid, "PYTHON_BIN": sys.executable})
        payload = json.dumps({"cwd": str(sandbox), "session_id": sid})

        expected = {
            "SessionStart": "Smoke plan",
            "UserPromptSubmit": "Smoke plan",
            "PreToolUse": "Smoke plan",
            "PermissionRequest": "Review the current phase",
            "PostToolUse": "Update progress.md",
            "PreCompact": "PreCompact: context compaction",
            "Stop": "Task in progress (0/1 phases complete)",
        }
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                      "PostToolUse", "PreCompact", "Stop"):
            output = run_event(event, events, sandbox, payload, env)
            require(expected[event] in output, f"{event} missed expected behavior: {output!r}")
            # PostToolUse throttles once per turn; each event is called once here.
            if event in {"SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PreCompact", "Stop"}:
                require("Smoke plan" in decode_output(output, event) or event in {"PermissionRequest", "PreCompact", "Stop"},
                        f"{event} lost active planning context")

        # Malformed input and a project without an active plan must not inject context.
        empty = Path(scratch) / "empty workspace"
        empty.mkdir()
        shutil.copytree(TEMPLATE / ".codex", empty / ".codex")
        shutil.copytree(TEMPLATE / ".agents", empty / ".agents")
        no_plan_env = env.copy()
        no_plan_env.pop("PWF_SESSION_ID", None)
        for malformed, empty_payload in (("{malformed", "malformed"),
                                         (json.dumps({"cwd": str(empty)}), "valid no-session")):
            for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                          "PostToolUse", "PreCompact", "Stop"):
                output = run_event(event, events, empty, malformed, no_plan_env)
                if event == "PreToolUse":
                    require(not output or json.loads(output).get("decision") == "allow",
                            "PreToolUse must safely no-op or allow without a plan")
                    continue
                require(not output, f"{event} should safely no-op ({empty_payload}): {output!r}")
        checks.append("7 hook events with attached plan; malformed and valid no-plan no-session no-op; paths with spaces/non-repo cwd")
    print("PASS: " + "; ".join(checks))


if __name__ == "__main__":
    main()
