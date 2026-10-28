#!/usr/bin/env python3
"""Smoke test for anti-entropy governance: AST-based delete-first verification and diff hygiene."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from loopx.capabilities.anti_entropy.cli import run_anti_entropy_verify
from loopx.capabilities.anti_entropy.contract import (
    ANTI_ENTROPY_CAPABILITY_ID,
    RetirementBasis,
)
from loopx.capabilities.anti_entropy.oracles import evaluate_diff_hygiene


def test_clean_delete_first() -> None:
    diff = """
--- a/calculator.py
+++ b/calculator.py
@@ -1,5 +1,5 @@
-def old_add(a: int, b: int) -> int:
-    return a + b
+def new_add(a: int, b: int) -> int:
+    return a + b
"""
    basis = RetirementBasis(
        target_symbols=("old_add",),
        rationale="Replaced by new_add",
    )
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "passed", f"expected passed, got {receipt.verdict}"
    assert "old_add" in receipt.retired_symbols


def test_delete_first_violation() -> None:
    diff = """
--- a/calculator.py
+++ b/calculator.py
@@ -1,3 +1,5 @@
+def new_add(a: int, b: int) -> int:
+    return a + b
 def old_add(a: int, b: int) -> int:
     return a + b
"""
    basis = RetirementBasis(
        target_symbols=("old_add",),
        allow_compat_seam=False,
    )
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "rejected", f"expected rejected, got {receipt.verdict}"
    assert any(f.rule_id == "AE001-DELETE_FIRST_VIOLATION" for f in receipt.findings)


def test_zombie_reintroduced() -> None:
    diff = """
--- a/calculator.py
+++ b/calculator.py
@@ -1,3 +1,3 @@
-def legacy_sink():
+def legacy_sink():  # wrapper
     pass
"""
    basis = RetirementBasis(target_symbols=("legacy_sink",))
    receipt = evaluate_diff_hygiene(diff, basis)
    assert receipt.verdict == "rejected", f"expected rejected, got {receipt.verdict}"
    assert any(f.rule_id == "AE002-ZOMBIE_SYMBOL_REINTRODUCED" for f in receipt.findings)


def test_cli_runner() -> None:
    diff = """
--- a/module.py
+++ b/module.py
@@ -1,2 +1,2 @@
-class OldService:
+class NewService:
     pass
"""
    payload = run_anti_entropy_verify(
        diff_text=diff,
        basis_data={"target_symbols": ["OldService"]},
        goal_id="test-goal-1",
    )
    assert payload["ok"] is True
    assert payload["capability_id"] == ANTI_ENTROPY_CAPABILITY_ID
    assert "OldService" in payload["retired_symbols"]


def main() -> int:
    test_clean_delete_first()
    test_delete_first_violation()
    test_zombie_reintroduced()
    test_cli_runner()
    print("anti-entropy-governance-smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
