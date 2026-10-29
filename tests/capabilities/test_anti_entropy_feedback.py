from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
