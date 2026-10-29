from __future__ import annotations

import json
from typing import Any

from loopx.capabilities.anti_entropy import (
    ANTI_ENTROPY_CAPABILITY_ID,
    ANTI_ENTROPY_CATALOG_ENTRY,
    ANTI_ENTROPY_RECEIPT_SCHEMA_VERSION,
    ANTI_ENTROPY_RETIREMENT_BASIS_SCHEMA_VERSION,
    AntiEntropyReceipt,
    RetirementBasis,
    audit_ghost_code,
    evaluate_diff_hygiene,
    run_anti_entropy_verify,
    verify_delete_first,
)
from loopx.capabilities.catalog import BUILTIN_CAPABILITIES


def test_catalog_registration() -> None:
    """Verify anti-entropy capability is registered in builtin capabilities catalog."""
    matching = [
        c for c in BUILTIN_CAPABILITIES if c["id"] == ANTI_ENTROPY_CAPABILITY_ID
    ]
    assert len(matching) == 1
    record = matching[0]
    assert record["origin"] == "builtin"
    assert record["status"] == "experimental"
    assert record["default_enabled"] is False
    assert "delete-first" in record["real_world_anchor"] or "refactoring" in record["real_world_anchor"]


def test_verify_delete_first_passed() -> None:
    """Verify clean retirement where old symbol is cleanly deleted."""
    diff = """
--- a/legacy_module.py
+++ b/legacy_module.py
@@ -1,5 +1,5 @@
-def old_deprecated_calculator(x, y):
-    return x + y
+def modern_calculator(x: int, y: int) -> int:
+    return x + y
"""
    basis = RetirementBasis(
        target_symbols=("old_deprecated_calculator",),
        rationale="Replaced by modern_calculator",
    )
    findings = verify_delete_first(diff, basis)
    assert len(findings) == 0

    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "passed"
    assert "old_deprecated_calculator" in receipt.retired_symbols


def test_verify_delete_first_violation() -> None:
    """Verify rejection when old symbol is kept instead of delete-first."""
    diff = """
--- a/legacy_module.py
+++ b/legacy_module.py
@@ -1,3 +1,6 @@
+def modern_calculator(x: int, y: int) -> int:
+    return x + y
 def old_deprecated_calculator(x, y):
     return x + y
"""
    basis = RetirementBasis(
        target_symbols=("old_deprecated_calculator",),
        allow_compat_seam=False,
    )
    findings = verify_delete_first(diff, basis)
    assert len(findings) == 1
    assert findings[0].rule_id == "AE001-DELETE_FIRST_VIOLATION"
    assert findings[0].severity == "blocking"

    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "rejected"


def test_verify_delete_first_zombie_reintroduced() -> None:
    """Verify rejection when symbol is deleted and reintroduced simultaneously."""
    diff = """
--- a/legacy_module.py
+++ b/legacy_module.py
@@ -1,3 +1,3 @@
-def legacy_sink():
+def legacy_sink():  # wrapper
     pass
"""
    basis = RetirementBasis(target_symbols=("legacy_sink",))
    findings = verify_delete_first(diff, basis)
    assert any(f.rule_id == "AE002-ZOMBIE_SYMBOL_REINTRODUCED" for f in findings)


def test_target_paths_deletion() -> None:
    """Verify target_paths require complete file deletion headers in the diff."""
    # File cleanly deleted -> pass
    deleted_diff = """
--- a/legacy/old_file.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def dead():
-    pass
"""
    basis = RetirementBasis(target_paths=("legacy/old_file.py",))
    receipt = evaluate_diff_hygiene(deleted_diff, basis)
    assert receipt.verdict == "passed"
    assert len(receipt.findings) == 0

    # File only partially modified instead of deleted -> fail
    partial_diff = """
--- a/legacy/old_file.py
+++ b/legacy/old_file.py
@@ -1,3 +1,3 @@
-def dead():
+def modified():
     pass
"""
    receipt_partial = evaluate_diff_hygiene(partial_diff, basis)
    assert receipt_partial.verdict == "rejected"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" for f in receipt_partial.findings)


def test_compat_seam_does_not_falsely_retire() -> None:
    """Verify allow_compat_seam allows passing but does NOT claim un-deleted symbols as retired."""
    diff = """
--- a/legacy_module.py
+++ b/legacy_module.py
@@ -1,3 +1,4 @@
+# some edit
 def old_api():
     return 1
"""
    basis = RetirementBasis(
        target_symbols=("old_api",),
        allow_compat_seam=True,
    )
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "passed"
    # old_api was NOT deleted, so it must NOT be in retired_symbols!
    assert "old_api" not in receipt.retired_symbols


def test_false_positive_prevention() -> None:
    """Ensure normal English comments or documentation mentioning words like fallback are not blocked."""
    diff = """
--- a/README.md
+++ b/README.md
@@ -10,3 +10,5 @@
+# if the connection is closed, the next call reconnects.
+# Please see documentation for details.
+# Delete fallback_v4 after migrating all callers.
"""
    basis = RetirementBasis()
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "passed"
    # Should not produce blocking findings
    assert all(f.severity != "blocking" for f in receipt.findings)


def test_audit_ghost_code_commented_block() -> None:
    """Verify advisory finding when dead code blocks are left behind as comments."""
    diff = """
--- a/service.py
+++ b/service.py
@@ -10,3 +10,8 @@
+def active_fn():
+    # def old_worker():
+    #     x = 1
+    #     return x
+    return True
"""
    findings = audit_ghost_code(diff)
    assert len(findings) == 1
    assert findings[0].rule_id == "AE003-COMMENTED_CODE_ADVISORY"
    assert findings[0].severity == "advisory"


def test_cli_runner() -> None:
    """Verify CLI verify function generates a complete, valid payload."""
    clean_diff = """
--- a/service.py
+++ b/service.py
@@ -1,3 +1,3 @@
-def legacy_call():
+def modern_call():
     return 42
"""
    payload = run_anti_entropy_verify(
        diff_text=clean_diff,
        basis_data={"target_symbols": ["legacy_call"]},
        goal_id="test-goal-id",
    )
    assert payload["ok"] is True
    assert payload["verdict"] == "passed"
    assert payload["blocking_finding_count"] == 0
    assert "legacy_call" in payload["retired_symbols"]

def test_retirement_basis_type_validation() -> None:
    """Verify strict type validation on RetirementBasis.from_dict to prevent truthiness loopholes."""
    import pytest

    # allow_compat_seam must be a real boolean, not truthy string "false"
    with pytest.raises(TypeError, match="allow_compat_seam must be a boolean"):
        RetirementBasis.from_dict({"allow_compat_seam": "false"})

    with pytest.raises(TypeError, match="allow_compat_seam must be a boolean"):
        RetirementBasis.from_dict({"allow_compat_seam": "true"})

    with pytest.raises(TypeError, match="allow_compat_seam must be a boolean"):
        RetirementBasis.from_dict({"allow_compat_seam": 1})

    # basis data must be a Mapping, not an array
    with pytest.raises(TypeError, match="must be a JSON object"):
        RetirementBasis.from_dict([])

    with pytest.raises(TypeError, match="must be a JSON object"):
        RetirementBasis.from_dict("not_a_dict")

    # target_symbols must be a collection of strings
    with pytest.raises(TypeError, match="target_symbols must be a list of strings"):
        RetirementBasis.from_dict({"target_symbols": "old_api"})

    with pytest.raises(TypeError, match="target_symbols elements must be strings"):
        RetirementBasis.from_dict({"target_symbols": [123]})

    # target_paths must be a collection of strings
    with pytest.raises(TypeError, match="target_paths must be a list of strings"):
        RetirementBasis.from_dict({"target_paths": "legacy.py"})

    with pytest.raises(TypeError, match="target_paths elements must be strings"):
        RetirementBasis.from_dict({"target_paths": [456]})

    # schema_version and rationale must be strings
    with pytest.raises(TypeError, match="schema_version must be a string"):
        RetirementBasis.from_dict({"schema_version": 123})

    with pytest.raises(TypeError, match="rationale must be a string"):
        RetirementBasis.from_dict({"rationale": 123})


def test_cli_exit_code_and_subprocess() -> None:
    """Verify CLI returns exit code 1 on rejection, 0 on pass, and 2 on invalid basis JSON."""
    import subprocess
    import sys

    # 1. Rejected verification -> exit code 1
    diff_fail = """
--- a/service.py
+++ b/service.py
@@ -1,3 +1,3 @@
+# keep old_api
 def old_api():
     return 1
"""
    proc_fail = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "anti-entropy",
            "verify",
            "--diff-file",
            "-",
            "--basis-json",
            "{\"target_symbols\": [\"old_api\"]}",
            "--format",
            "json",
        ],
        input=diff_fail.encode("utf-8"),
        capture_output=True,
    )
    assert proc_fail.returncode == 1

    # 2. Passed verification -> exit code 0
    diff_pass = """
--- a/service.py
+++ b/service.py
@@ -1,3 +1,3 @@
-def old_api():
-    return 1
+def new_api():
+    return 2
"""
    proc_pass = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "anti-entropy",
            "verify",
            "--diff-file",
            "-",
            "--basis-json",
            "{\"target_symbols\": [\"old_api\"]}",
            "--format",
            "json",
        ],
        input=diff_pass.encode("utf-8"),
        capture_output=True,
    )
    assert proc_pass.returncode == 0

    # 3. Invalid basis JSON array -> exit code 2
    proc_err = subprocess.run(
        [
            sys.executable,
            "-m",
            "loopx.cli",
            "anti-entropy",
            "verify",
            "--diff-file",
            "-",
            "--basis-json",
            "[]",
            "--format",
            "json",
        ],
        input=b"",
        capture_output=True,
    )
    assert proc_err.returncode == 2


def test_docstring_example_deletion_does_not_retire_real_definition() -> None:
    """Verify deleting a code example in a docstring does not falsely pass a retained real definition."""
    diff = (
        "--- a/api.py\n"
        "+++ b/api.py\n"
        "@@ -1,6 +1,3 @@\n"
        " \"\"\"\n"
        "-Example:\n"
        "-    def old_api():\n"
        "-        return 1\n"
        " \"\"\"\n"
        " def old_api():\n"
        "     return 2\n"
    )
    basis = RetirementBasis(target_symbols=("old_api",))
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "rejected"
    assert any(f.rule_id in ("AE001-DELETE_FIRST_VIOLATION", "AE002-ZOMBIE_SYMBOL_REINTRODUCED") for f in receipt.findings)
    assert "old_api" not in receipt.retired_symbols


def test_exact_git_path_confirmation_and_encoding() -> None:
    """Verify exact path matching without suffix guesswork, plus empty and quoted path deletion."""
    # 1. Suffix mismatch: deleting legacy_api.py must NOT satisfy target_paths=("api.py",)
    diff_suffix = """
--- a/legacy_api.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def f(): pass
"""
    basis_suffix = RetirementBasis(target_paths=("api.py",))
    receipt_suffix = evaluate_diff_hygiene(diff_suffix, basis_suffix)
    assert receipt_suffix.verdict == "rejected"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" for f in receipt_suffix.findings)

    # 2. Empty file deletion metadata (deleted file mode 100644 without +/- hunks)
    diff_empty = """
diff --git a/legacy/__init__.py b/legacy/__init__.py
deleted file mode 100644
index e69de29..0000000
"""
    basis_empty = RetirementBasis(target_paths=("legacy/__init__.py",))
    receipt_empty = evaluate_diff_hygiene(diff_empty, basis_empty)
    assert receipt_empty.verdict == "passed"
    assert len(receipt_empty.findings) == 0

    # 3. Quoted path with C-style octal escaping for non-ASCII paths
    diff_quoted = """
diff --git "a/legacy/\\346\\227\\247\\345\\256\\236\\347\\214\\233.py" "b/legacy/\\346\\227\\247\\345\\256\\236\\347\\214\\233.py"
deleted file mode 100644
--- "a/legacy/\\346\\227\\247\\345\\256\\236\\347\\214\\233.py"
+++ /dev/null
"""
    basis_quoted = RetirementBasis(target_paths=("legacy/旧实猛.py",))
    receipt_quoted = evaluate_diff_hygiene(diff_quoted, basis_quoted)
    assert receipt_quoted.verdict == "passed"
    assert len(receipt_quoted.findings) == 0


def test_reintroduced_symbol_excluded_from_retired_symbols_with_partial_retirement() -> None:
    """Verify modified/reintroduced symbol is excluded from retired_symbols while cleanly retired symbol is kept."""
    diff = """
--- a/service.py
+++ b/service.py
@@ -1,6 +1,4 @@
-def old_helper():
-    return 1
-def old_api(x):
-    return x
+def old_api(x=1):
+    return x
"""
    basis = RetirementBasis(target_symbols=("old_helper", "old_api"))
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "rejected"
    assert any(f.rule_id == "AE002-ZOMBIE_SYMBOL_REINTRODUCED" and f.location == "old_api" for f in receipt.findings)
    # old_api is excluded from retired_symbols, but old_helper is accurately certified!
    assert receipt.retired_symbols == ("old_helper",)


def test_typescript_clean_delete_first() -> None:
    """Verify TypeScript functions, classes, interfaces, and types are accurately recognized as retired."""
    diff = """
--- a/control_plane.ts
+++ b/control_plane.ts
@@ -1,15 +1,2 @@
-export function oldLeaseAcquire(taskId: string): boolean {
-    return false;
-}
-export class OldScheduler {
-    run(): void {}
-}
-export interface OldLeaseConfig {
-    timeoutMs: number;
-}
-export type OldLeaseState = "active" | "expired";
-export const oldArrowHelper = (x: number) => x * 2;
+export function newLeaseAcquire(): boolean { return true; }
"""
    basis = RetirementBasis(
        target_symbols=(
            "oldLeaseAcquire",
            "OldScheduler",
            "OldLeaseConfig",
            "OldLeaseState",
            "oldArrowHelper",
        ),
    )
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "passed", f"findings: {receipt.findings}"
    assert set(receipt.retired_symbols) == {
        "oldLeaseAcquire",
        "OldScheduler",
        "OldLeaseConfig",
        "OldLeaseState",
        "oldArrowHelper",
    }


def test_typescript_call_deletion_does_not_retire_definition() -> None:
    """Verify deleting a TypeScript function call without deleting definition triggers AE001."""
    diff = """
--- a/control_plane.ts
+++ b/control_plane.ts
@@ -1,7 +1,7 @@
 export function oldLeaseAcquire(): boolean {
     return false;
 }
 export function run() {
-    return oldLeaseAcquire();
+    return newLeaseAcquire();
 }
"""
    basis = RetirementBasis(target_symbols=("oldLeaseAcquire",))
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "rejected"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" for f in receipt.findings)
    assert "oldLeaseAcquire" not in receipt.retired_symbols


def test_typescript_zombie_symbol_reintroduced() -> None:
    """Verify reintroducing an old TypeScript function as a wrapper triggers AE002."""
    diff = """
--- a/control_plane.ts
+++ b/control_plane.ts
@@ -1,4 +1,4 @@
-export function oldTaskLease() { return 1; }
+export function oldTaskLease() { return newTaskLease(); }
"""
    basis = RetirementBasis(target_symbols=("oldTaskLease",))
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "rejected"
    assert any(f.rule_id == "AE002-ZOMBIE_SYMBOL_REINTRODUCED" for f in receipt.findings)


def test_offline_self_contained_diff_without_disk_access() -> None:
    """Verify diff verification is self-contained and works for non-existent disk paths."""
    diff = """
--- a/non_existent_dir/virtual_module.py
+++ b/non_existent_dir/virtual_module.py
@@ -1,5 +1,2 @@
-def obsolete_worker():
-    return None
+def active_worker(): return 1
"""
    basis = RetirementBasis(target_symbols=("obsolete_worker",))
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "passed"
    assert receipt.retired_symbols == ("obsolete_worker",)


def test_agent_tool_inspect_interface() -> None:
    """Verify the structured agent tool anti_entropy_inspect returns complete, valid payload."""
    from loopx.capabilities.anti_entropy.agent_tool import anti_entropy_inspect

    diff = """
--- a/service.ts
+++ b/service.ts
@@ -1,4 +1,2 @@
-export function legacyHandler() {}
+export function modernHandler() {}
"""
    payload = anti_entropy_inspect(
        diff_text=diff,
        retirement_basis={"target_symbols": ["legacyHandler"]},
        goal_id="goal-test-tool",
    )
    assert payload["ok"] is True
    assert payload["command"] == "verify"
    assert payload["goal_id"] == "goal-test-tool"
    assert payload["retired_symbols"] == ["legacyHandler"]
    assert payload["verdict"] == "passed"


def test_path_normalization_distinguishes_a_dir_and_dot_files() -> None:
    """Verify a/api.py, api.py, and .api.py are never conflated."""
    diff_delete_root = """
--- a/api.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def f(): pass
"""
    basis_a = RetirementBasis(target_paths=("a/api.py",))
    receipt_a = evaluate_diff_hygiene(diff_delete_root, basis_a)
    assert receipt_a.verdict == "rejected"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" and f.location == "a/api.py" for f in receipt_a.findings)

    basis_dot = RetirementBasis(target_paths=(".api.py",))
    receipt_dot = evaluate_diff_hygiene(diff_delete_root, basis_dot)
    assert receipt_dot.verdict == "rejected"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" and f.location == ".api.py" for f in receipt_dot.findings)

    diff_delete_nested_a = """
--- a/a/api.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def f(): pass
"""
    receipt_nested = evaluate_diff_hygiene(diff_delete_nested_a, basis_a)
    assert receipt_nested.verdict == "passed"
    assert len(receipt_nested.findings) == 0

    diff_delete_dot = """
--- a/.api.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def f(): pass
"""
    receipt_dot_pass = evaluate_diff_hygiene(diff_delete_dot, basis_dot)
    assert receipt_dot_pass.verdict == "passed"
    assert len(receipt_dot_pass.findings) == 0


def test_empty_file_deletion_with_spaces_in_filename() -> None:
    """Verify unquoted git diff headers for empty files with spaces in names are correctly recognized."""
    diff_spaces = """
diff --git a/legacy/old api.py b/legacy/old api.py
deleted file mode 100644
index e69de29..0000000
"""
    basis = RetirementBasis(target_paths=("legacy/old api.py",))
    receipt = evaluate_diff_hygiene(diff_spaces, basis)
    assert receipt.verdict == "passed", f"findings: {receipt.findings}"
    assert len(receipt.findings) == 0


def test_docstring_sample_not_certified_when_function_retained_on_disk(tmp_path: Path, monkeypatch) -> None:
    """Verify deleting docstring example lines does not falsely certify a function retained on disk, even from subdirs."""
    repo_root = tmp_path / "mock_repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    subdir = repo_root / "nested" / "sub"
    subdir.mkdir(parents=True)

    module_file = repo_root / "service.py"
    module_file.write_text(
        """\"\"\"\nExample:\n    def old_api():\n        pass\n\"\"\"\n\ndef old_api():\n    return 42\n""",
        encoding="utf-8",
    )

    diff_docstring = """
--- a/service.py
+++ b/service.py
@@ -3,2 +3,0 @@
-    def old_api():
-        pass
"""
    basis = RetirementBasis(target_symbols=("old_api",))

    monkeypatch.chdir(repo_root)
    receipt_root = evaluate_diff_hygiene(diff_docstring, basis)
    assert receipt_root.verdict == "rejected"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" for f in receipt_root.findings)
    assert "old_api" not in receipt_root.retired_symbols

    monkeypatch.chdir(subdir)
    receipt_sub = evaluate_diff_hygiene(diff_docstring, basis)
    assert receipt_sub.verdict == "rejected"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" for f in receipt_sub.findings)
    assert "old_api" not in receipt_sub.retired_symbols
