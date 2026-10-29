from __future__ import annotations

from typing import Any

from .cli import run_anti_entropy_verify


def anti_entropy_inspect(
    *,
    diff_text: str,
    retirement_basis: dict[str, Any] | None = None,
    goal_id: str | None = None,
) -> dict[str, Any]:
    """Inspect unified diff against a declared retirement basis and diff hygiene rules.

    This tool allows autonomous agents to verify that deprecated symbols and files
    are genuinely decommissioned (delete-first) and that no ghost code or zombie
    wrappers are reintroduced, returning an immutable structured receipt.
    """
    return run_anti_entropy_verify(
        diff_text=diff_text,
        basis_data=retirement_basis,
        goal_id=goal_id,
    )
