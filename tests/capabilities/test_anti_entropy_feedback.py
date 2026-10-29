from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from loopx.capabilities.anti_entropy import (
    ANTI_ENTROPY_CAPABILITY_ID,
    ANTI_ENTROPY_CATALOG_ENTRY,
    ANTI_ENTROPY_INSPECT_TOOL_DESCRIPTOR,
    build_anti_entropy_turn_context,
    run_anti_entropy_feedback_cycle,
)

CLEAN_DIFF = """
--- a/service.py
+++ b/service.py
@@ -1,3 +1,2 @@
-def old_api():
-    return 1
+pass
"""

VIOLATION_DIFF = """
--- a/service.py
+++ b/service.py
@@ -1,4 +1,5 @@
+def new_api():
+    return 2
 def old_api():
     return 1
"""

BASIS = {"target_symbols": ["old_api"], "rationale": "replaced by new_api"}


def _make_workspace(tmp_path: Path, content: str) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "service.py").write_text(content, encoding="utf-8")
    return workspace


def test_capability_is_default_off() -> None:
    """The feedback loop ships disabled: enabling is an explicit task-scoped choice."""
    assert ANTI_ENTROPY_CATALOG_ENTRY["default_enabled"] is False


def test_inspect_tool_descriptor_is_well_formed() -> None:
    """The agent tool descriptor names required inputs and routes."""
    assert ANTI_ENTROPY_INSPECT_TOOL_DESCRIPTOR["name"] == "anti_entropy_inspect"
    schema = ANTI_ENTROPY_INSPECT_TOOL_DESCRIPTOR["input_schema"]
    assert schema["required"] == ["diff_text"]
    assert "workspace" in schema["properties"]
    assert "--workspace" in ANTI_ENTROPY_INSPECT_TOOL_DESCRIPTOR["cli_route"]
    # The catalog registers the tool surface so hosts can discover it.
    agent_tools = ANTI_ENTROPY_CATALOG_ENTRY["agent_tools"]
    assert any(t["name"] == "anti_entropy_inspect" for t in agent_tools)


def test_turn_context_projects_declared_task(tmp_path: Path) -> None:
    """The turn context carries the declared retirement goal and inspect route."""
    context = build_anti_entropy_turn_context(
        basis=BASIS, goal_id="goal-123", workspace=tmp_path
    )
    assert context["capability_id"] == ANTI_ENTROPY_CAPABILITY_ID
    assert context["goal_id"] == "goal-123"
    assert context["task"]["target_symbols"] == ["old_api"]
    assert context["task"]["objective"] == "replaced by new_api"
    assert context["inspect_tool"]["name"] == "anti_entropy_inspect"
    assert "change-quality" in context["feedback_semantics"]["delivery_gate"]
    # Building the context executes nothing.
    assert not list(tmp_path.iterdir())


def test_feedback_cycle_rejection_is_progress_fact(tmp_path: Path) -> None:
    """A retained definition yields actionable feedback, not a terminal failure."""
    workspace = _make_workspace(
        tmp_path, "def new_api():\n    return 2\n\ndef old_api():\n    return 1\n"
    )
    result = run_anti_entropy_feedback_cycle(
        diff_text=VIOLATION_DIFF, basis=BASIS, goal_id="goal-123", workspace=workspace
    )
    assert result["ok"] is True
    assert result["verdict"] == "rejected"
    assert result["is_progress_fact"] is True
    assert result["delivery_ready"] is False
    feedback = result["actionable_feedback"]
    assert feedback, "expected actionable feedback entries"
    ae001 = [f for f in feedback if f["rule_id"].startswith("AE001")]
    assert ae001 and ae001[0]["next_action"]
    assert "Remove" in ae001[0]["next_action"]
    # The route for final acceptance is named but not executed here.
    assert "change-quality" in result["delivery_route"]
    # Read-only boundary: no receipt store or files written by the cycle.
    assert sorted(p.name for p in workspace.iterdir()) == ["service.py"]


def test_feedback_cycle_reports_missing_workspace(tmp_path: Path) -> None:
    """AE004 feedback tells the agent to bind the workspace and re-run."""
    result = run_anti_entropy_feedback_cycle(diff_text=CLEAN_DIFF, basis=BASIS)
    assert result["verdict"] == "rejected"
    assert result["is_progress_fact"] is True
    ae004 = [f for f in result["actionable_feedback"] if f["rule_id"].startswith("AE004")]
    assert ae004
    assert "workspace" in ae004[0]["next_action"]


def test_feedback_cycle_delivery_ready(tmp_path: Path) -> None:
    """A passed, source-bound round covering all targets marks delivery-ready."""
    workspace = _make_workspace(tmp_path, "pass\n")
    result = run_anti_entropy_feedback_cycle(
        diff_text=CLEAN_DIFF, basis=BASIS, goal_id="goal-123", workspace=workspace
    )
    assert result["verdict"] == "passed"
    assert result["delivery_ready"] is True
    assert result["is_progress_fact"] is False
    assert result["retired_symbols"] == ["old_api"]
    assert result["receipt"]["source_evidence"] == "workspace_sources"


def test_inspect_cli_roundtrip(tmp_path: Path) -> None:
    """The CLI inspect subcommand is a real callable entry for agents."""
    workspace = _make_workspace(
        tmp_path, "def new_api():\n    return 2\n\ndef old_api():\n    return 1\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "anti-entropy",
            "inspect",
            "--diff-file",
            "-",
            "--basis-json",
            json.dumps(BASIS),
            "--workspace",
            str(workspace),
            "--goal-id",
            "goal-123",
            "--format",
            "json",
        ],
        input=VIOLATION_DIFF.encode("utf-8"),
        capture_output=True,
    )
    # Rejection is a progress fact: the check itself succeeds (exit 0).
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["command"] == "inspect"
    assert payload["verdict"] == "rejected"
    assert payload["is_progress_fact"] is True
    assert payload["actionable_feedback"]

    # Verify remains the delivery-gate entry with non-zero rejection exit.
    proc_verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "anti-entropy",
            "verify",
            "--diff-file",
            "-",
            "--basis-json",
            json.dumps(BASIS),
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
        input=VIOLATION_DIFF.encode("utf-8"),
        capture_output=True,
    )
    assert proc_verify.returncode == 1


def test_delivery_route_has_valid_change_quality_parameters() -> None:
    """Verify delivery route command matches loopx change-quality CLI syntax (--result-json, --execute)."""
    from loopx.capabilities.anti_entropy.feedback import _DELIVERY_ROUTE
    assert "--result-json" in _DELIVERY_ROUTE
    assert "--execute" in _DELIVERY_ROUTE
    assert "--result-path" not in _DELIVERY_ROUTE
    assert "prepare" in _DELIVERY_ROUTE
    assert "verify" in _DELIVERY_ROUTE


def test_delivery_route_executes_real_change_quality_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The emitted delivery route executes against the real change-quality owner.

    Full journey on a real Git retirement change: the anti-entropy inspect
    round marks the change delivery-ready, then the route's prepare →
    record (--result-json + --execute) → verify commands run through the real
    CLI parser and handler, and any later diff change invalidates the stored
    receipt (requalification_required).
    """
    import subprocess as sp

    from loopx.capabilities.change_quality.receipt import (
        CHANGE_QUALITY_RESULT_SCHEMA_VERSION,
    )
    from loopx.cli import main
    from loopx.configure_goal import configure_goal

    goal_id = "anti-entropy-delivery-fixture"

    def git(repo: Path, *args: str) -> str:
        return sp.run(
            ["git", "-C", str(repo), *args],
            check=True, text=True,
            stdout=sp.PIPE, stderr=sp.PIPE,
        ).stdout.strip()

    # Real Git fixture: committed old_api + caller, then a full retirement change.
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "anti-entropy@example.invalid")
    git(repo, "config", "user.name", "Anti Entropy Fixture")
    (repo / "service.py").write_text(
        "def old_api():\n    return 1\n", encoding="utf-8"
    )
    (repo / "client.py").write_text(
        "from service import old_api\n\ndef run():\n    return old_api()\n",
        encoding="utf-8",
    )
    git(repo, "add", "service.py", "client.py")
    git(repo, "commit", "-m", "fixture with legacy api")
    # Post-change sources: migration completed, old_api fully removed.
    (repo / "service.py").write_text(
        "def new_api():\n    return 2\n", encoding="utf-8"
    )
    (repo / "client.py").write_text(
        "from service import new_api\n\ndef run():\n    return new_api()\n",
        encoding="utf-8",
    )
    real_diff = git(repo, "diff", "HEAD")

    registry = tmp_path / "registry.json"
    runtime_root = tmp_path / "runtime"
    registry.write_text(
        json.dumps(
            {
                "common_runtime_root": str(runtime_root),
                "goals": [
                    {"id": goal_id, "repo": str(repo), "control_plane": {}}
                ],
            }
        ),
        encoding="utf-8",
    )
    configure_goal(
        registry_path=registry,
        goal_id=goal_id,
        change_quality_enabled=True,
        change_quality_safe_fix=True,
        change_quality_strict_receipt=True,
        execute=True,
    )

    # 1. Anti-entropy inspect round: delivery-ready on the real post-change tree.
    cycle = run_anti_entropy_feedback_cycle(
        diff_text=real_diff, basis=BASIS, goal_id=goal_id, workspace=repo
    )
    assert cycle["verdict"] == "passed", f"findings: {cycle['actionable_feedback']}"
    assert cycle["delivery_ready"] is True
    assert cycle["retired_symbols"] == ["old_api"]

    # The route template this round hands to the delivery owner.
    from loopx.capabilities.anti_entropy.feedback import _DELIVERY_ROUTE

    scope_args = ["--repo-path", str(repo), "--base-ref", "HEAD"]

    # 2. Route step 1: prepare (real CLI parser + handler).
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    assert main(
        ["--registry", str(registry), "--runtime-root", str(runtime_root),
         "--format", "json", "change-quality", "prepare",
         "--goal-id", goal_id, *scope_args]
    ) == 0
    prepared = json.loads(capsys.readouterr().out)
    fingerprint = prepared["scope"]["scope_fingerprint"]
    assert fingerprint

    # 3. Route step 2: record with --result-json (change_quality_agent_result_v2).
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "schema_version": CHANGE_QUALITY_RESULT_SCHEMA_VERSION,
                "scope_fingerprint": fingerprint,
                "reviewed_final_scope": True,
                "reuse": {
                    "outcome": "retained",
                    "summary": "Fixture keeps the direct owner.",
                    "evidence_refs": ["path:service.py", "path:client.py"],
                },
                "simplification": {
                    "outcome": "retained",
                    "summary": "Migration already minimal.",
                    "evidence_refs": ["path:service.py", "path:client.py"],
                    "safe_fix_applied": False,
                },
                "risks": [],
                "validation": [
                    {
                        "validator": "anti-entropy-inspect",
                        "status": "passed",
                        "scope": "retirement targets certified on bound sources",
                        "required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert main(
        ["--registry", str(registry), "--runtime-root", str(runtime_root),
         "--format", "json", "change-quality", "record",
         "--goal-id", goal_id, *scope_args,
         "--result-json", str(result_path), "--execute"]
    ) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["decision"] == "pass"
    assert recorded["receipt_path"]

    # 4. Route step 3: verify — valid for the unchanged exact scope.
    capsys.readouterr()
    assert main(
        ["--registry", str(registry), "--runtime-root", str(runtime_root),
         "--format", "json", "change-quality", "verify",
         "--goal-id", goal_id, *scope_args]
    ) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "valid"
    assert verified["pr_evidence"]["requalification_required"] is False

    # 5. Invalidation counterexample: any diff change after record requires
    #    requalification — the receipt is not a permanent pass.
    (repo / "client.py").write_text(
        "from service import new_api\n\ndef run():\n    return new_api() + 1\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert main(
        ["--registry", str(registry), "--runtime-root", str(runtime_root),
         "--format", "json", "change-quality", "verify",
         "--goal-id", goal_id, *scope_args]
    ) == 1
    stale = json.loads(capsys.readouterr().out)
    assert stale["ok"] is False
    assert stale["pr_evidence"]["requalification_required"] is True

    # The executed tokens are exactly the route's contract: the string is a
    # template of real, parser-accepted parameters.
    for token in ("--result-json", "--execute", "prepare", "record", "verify"):
        assert token in _DELIVERY_ROUTE


def test_turn_context_hook_lifecycle_all_states(tmp_path: Path) -> None:
    """Verify turn-context hook across all 4 lifecycle states: disabled, non-applicable, new task, and recovery."""
    from loopx.capabilities.anti_entropy.turn_context import (
        anti_entropy_context_registration,
        anti_entropy_context_result,
    )
    from loopx.control_plane.capability_hooks import (
        dispatch_turn_context_hooks,
        project_turn_context,
    )

    # State 1: Disabled
    payload_disabled = {"goal_id": "g-1", "agent_id": "a-1"}
    decision_disabled = {}
    res_disabled = anti_entropy_context_result(
        payload_disabled, goal_id="g-1", decision=decision_disabled
    )
    assert res_disabled["readback"]["anti_entropy_guidance"] is None
    assert res_disabled["context_fields"] == {}
    assert res_disabled["instructions"] == []

    # State 2: Enabled but not applicable (no retirement targets declared)
    payload_na = {"goal_id": "g-1", "agent_id": "a-1", "anti_entropy_enabled": True}
    res_na = anti_entropy_context_result(
        payload_na, goal_id="g-1", decision={}
    )
    assert res_na["readback"]["anti_entropy_guidance"]["enabled"] is True
    assert res_na["readback"]["anti_entropy_guidance"]["applicable"] is False
    assert res_na["context_fields"] == {}

    # State 3: Active new task (retirement basis declared)
    basis_data = {"target_symbols": ["old_service"], "rationale": "migrate to v2"}
    payload_active = {
        "goal_id": "g-1",
        "agent_id": "a-1",
        "retirement_basis": basis_data,
        "workspace": str(tmp_path),
    }
    res_active = anti_entropy_context_result(
        payload_active, goal_id="g-1", decision={}, workspace=tmp_path
    )
    guidance = res_active["readback"]["anti_entropy_guidance"]
    assert guidance["enabled"] is True
    assert guidance["applicable"] is True
    assert guidance["status"] == "active"
    assert "anti_entropy_context" in res_active["context_fields"]
    assert any("delete-first" in inst.lower() for inst in res_active["instructions"])

    # State 4: Failure recovery
    payload_recovery = {
        "goal_id": "g-1",
        "agent_id": "a-1",
        "retirement_basis": basis_data,
        "anti_entropy_progress_fact": True,
        "workspace": str(tmp_path),
    }
    res_recovery = anti_entropy_context_result(
        payload_recovery, goal_id="g-1", decision={}, workspace=tmp_path
    )
    guidance_rec = res_recovery["readback"]["anti_entropy_guidance"]
    assert guidance_rec["status"] == "recovering"
    assert any("recovery" in inst.lower() for inst in res_recovery["instructions"])

    # End-to-end dispatch through capability hooks
    reg = anti_entropy_context_registration(
        payload_active,
        registry_path=tmp_path,
        goal_id="g-1",
        agent_id="a-1",
        decision={},
        turn_instance_id="t-1",
    )
    payload_dispatched = dict(payload_active)
    dispatch_turn_context_hooks(payload_dispatched, (reg,))
    assert payload_dispatched.get("anti_entropy_guidance") is not None
    assert payload_dispatched["anti_entropy_guidance"]["status"] == "active"

    projected = project_turn_context(payload_dispatched)
    assert "anti_entropy_context" in projected.get("capability_context_fields", [])
    assert any("anti-entropy" in inst.lower() for inst in projected.get("capability_instructions", []))


def test_attach_turn_context_integration(tmp_path: Path) -> None:
    """Verify loopx.capabilities.turn_context.attach_turn_context wires anti-entropy with memory."""
    from loopx.capabilities.turn_context import attach_turn_context

    basis_data = {"target_symbols": ["old_api"], "rationale": "replaced"}
    payload = {
        "goal_id": "g-1",
        "agent_id": "a-1",
        "retirement_basis": basis_data,
        "workspace": str(tmp_path),
    }

    attach_turn_context(
        payload,
        registry_path=tmp_path,
        goal_id="g-1",
        agent_id="a-1",
        decision={},
        turn_instance_id="turn-42",
    )

    assert "anti_entropy_guidance" in payload
    assert payload["anti_entropy_guidance"]["status"] == "active"
    assert payload["anti_entropy_guidance"]["target_symbols"] == ["old_api"]
