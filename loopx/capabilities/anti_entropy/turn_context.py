"""Turn-context hook integration for anti-entropy code governance."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...control_plane.capability_hooks import TurnContextHookRegistration
from .contract import RetirementBasis
from .feedback import build_anti_entropy_turn_context

ANTI_ENTROPY_CONTEXT_MAX_RESULT_BYTES = 64 * 1024


def _extract_basis_candidate(candidate: Any) -> RetirementBasis | None:
    """Recursively search for a valid RetirementBasis in a candidate object or dictionary."""
    if candidate is None:
        return None
    if isinstance(candidate, RetirementBasis):
        return candidate if (candidate.target_symbols or candidate.target_paths) else None
    if isinstance(candidate, str):
        candidate_clean = candidate.strip()
        if (candidate_clean.startswith("{") and candidate_clean.endswith("}")) or (
            candidate_clean.startswith("[") and candidate_clean.endswith("]")
        ):
            try:
                parsed = json.loads(candidate_clean)
                return _extract_basis_candidate(parsed)
            except Exception:
                pass
        return None
    if isinstance(candidate, Mapping):
        # 1. Direct explicit retirement_basis or anti_entropy_basis keys
        for key in ("retirement_basis", "anti_entropy_basis"):
            if key in candidate:
                b = _extract_basis_candidate(candidate[key])
                if b is not None:
                    return b

        # 2. anti_entropy block
        if "anti_entropy" in candidate and isinstance(candidate["anti_entropy"], Mapping):
            b = _extract_basis_candidate(candidate["anti_entropy"])
            if b is not None:
                return b

        # 3. Containers commonly holding task params or metadata
        for container_key in ("metadata", "params", "task_spec", "task_parameters"):
            if container_key in candidate:
                b = _extract_basis_candidate(candidate[container_key])
                if b is not None:
                    return b

        # 4. Direct basis shape (target_symbols or target_paths)
        if "target_symbols" in candidate or "target_paths" in candidate:
            try:
                b = RetirementBasis.from_dict(candidate)
                if b.target_symbols or b.target_paths:
                    return b
            except Exception:
                pass

        # 5. String fields that might store JSON payload
        for text_key in ("note", "description", "instruction"):
            text_val = candidate.get(text_key)
            if isinstance(text_val, str):
                b = _extract_basis_candidate(text_val)
                if b is not None:
                    return b

    return None


def _resolve_retirement_basis(
    payload: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> RetirementBasis | None:
    """Extract a declared retirement basis from turn payload, decision, or selected todo."""
    candidates = [
        # Top-level direct fields
        payload.get("retirement_basis"),
        payload.get("anti_entropy_basis"),
        payload.get("anti_entropy"),
        decision.get("retirement_basis"),
        decision.get("anti_entropy_basis"),
        decision.get("anti_entropy"),

        # Native route and turn_envelope selected todos
        payload.get("route", {}).get("selected_todo")
        if isinstance(payload.get("route"), Mapping) else None,
        payload.get("turn_envelope", {}).get("action", {}).get("selected_todo")
        if isinstance(payload.get("turn_envelope"), Mapping)
        and isinstance(payload.get("turn_envelope", {}).get("action"), Mapping) else None,
        payload.get("turn_envelope", {}).get("selected_todo")
        if isinstance(payload.get("turn_envelope"), Mapping) else None,

        # Top-level and task fields
        payload.get("selected_todo"),
        payload.get("task"),

        # Decision-side selected todos
        decision.get("selected_todo"),
        decision.get("action", {}).get("selected_todo")
        if isinstance(decision.get("action"), Mapping) else None,
        decision.get("route", {}).get("selected_todo")
        if isinstance(decision.get("route"), Mapping) else None,
        decision.get("task"),
    ]

    for cand in candidates:
        basis = _extract_basis_candidate(cand)
        if basis is not None:
            return basis

    return None


def _is_anti_entropy_enabled(
    payload: Mapping[str, Any],
    decision: Mapping[str, Any],
    basis: RetirementBasis | None,
) -> bool:
    """Determine whether anti-entropy capability is enabled for this turn."""
    # If an explicit retirement basis was declared, capability is implicitly enabled for that task
    if basis is not None and (basis.target_symbols or basis.target_paths):
        return True

    # Check direct payload and decision enablement
    if payload.get("anti_entropy_enabled") is True or decision.get("anti_entropy_enabled") is True:
        return True

    ae_payload = payload.get("anti_entropy")
    if isinstance(ae_payload, Mapping) and ae_payload.get("enabled") is True:
        return True

    ae_decision = decision.get("anti_entropy")
    if isinstance(ae_decision, Mapping) and ae_decision.get("enabled") is True:
        return True

    # Check route and turn_envelope boundary capabilities
    route = payload.get("route")
    if isinstance(route, Mapping) and route.get("anti_entropy_enabled") is True:
        return True

    envelope = payload.get("turn_envelope")
    if isinstance(envelope, Mapping):
        boundary = envelope.get("boundary")
        if isinstance(boundary, Mapping):
            caps = boundary.get("capabilities")
            if isinstance(caps, Mapping):
                ae_cap = caps.get("anti_entropy_governance") or caps.get("anti_entropy")
                if isinstance(ae_cap, Mapping) and ae_cap.get("enabled") is True:
                    return True
                if ae_cap is True:
                    return True

    # Check selected todos for anti_entropy_enabled flag
    todos_to_check = [
        payload.get("selected_todo"),
        payload.get("route", {}).get("selected_todo") if isinstance(payload.get("route"), Mapping) else None,
        payload.get("turn_envelope", {}).get("action", {}).get("selected_todo")
        if isinstance(payload.get("turn_envelope"), Mapping)
        and isinstance(payload.get("turn_envelope", {}).get("action"), Mapping) else None,
        decision.get("selected_todo"),
        decision.get("action", {}).get("selected_todo") if isinstance(decision.get("action"), Mapping) else None,
    ]
    for td in todos_to_check:
        if isinstance(td, Mapping):
            if td.get("anti_entropy_enabled") is True:
                return True
            meta = td.get("metadata")
            if isinstance(meta, Mapping) and meta.get("anti_entropy_enabled") is True:
                return True
            params = td.get("params")
            if isinstance(params, Mapping) and params.get("anti_entropy_enabled") is True:
                return True

    return False


def anti_entropy_context_result(
    payload: Mapping[str, Any],
    *,
    goal_id: str,
    decision: Mapping[str, Any],
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Produce the bounded turn-context payload covering the four lifecycle states.

    1. Disabled: capability is inactive.
    2. Enabled but not applicable: capability is active but task has no retirement targets.
    3. New task: declared targets projected into context + inspect tool + delete-first guidance.
    4. Failure recovery: previous diff had blocking findings; progress facts guide remediation.
    """
    basis = _resolve_retirement_basis(payload, decision)
    enabled = _is_anti_entropy_enabled(payload, decision, basis)

    # State 1: Disabled
    if not enabled:
        return {
            "readback": {"anti_entropy_guidance": None},
            "context_fields": {},
            "instructions": [],
            "result_fields": {},
        }

    # State 2: Enabled but not applicable
    if basis is None or (not basis.target_symbols and not basis.target_paths):
        return {
            "readback": {
                "anti_entropy_guidance": {
                    "enabled": True,
                    "applicable": False,
                }
            },
            "context_fields": {},
            "instructions": [],
            "result_fields": {},
        }

    # Determine whether this is a failure recovery turn
    recovery_evidence = (
        payload.get("anti_entropy_progress_fact")
        or payload.get("anti_entropy_last_result")
        or decision.get("anti_entropy_last_result")
    )
    is_recovery = bool(recovery_evidence)

    # State 3 / 4: Active task or failure recovery
    resolved_workspace = workspace or Path.cwd()
    context = build_anti_entropy_turn_context(
        basis=basis,
        goal_id=goal_id,
        workspace=resolved_workspace,
    )

    if is_recovery:
        instructions = [
            "Anti-entropy recovery: previous diff check identified unretired symbols or diff hygiene issues. Address blocking findings before attempting delivery.",
            "Run 'anti_entropy_inspect' or 'loopx anti-entropy inspect' to verify diff cleanliness; rejections are progress facts guiding the next correction.",
        ]
        status = "recovering"
    else:
        instructions = [
            "Anti-entropy code governance is active for this task: delete-first symbol and path retirement is required.",
            "Call 'anti_entropy_inspect' or 'loopx anti-entropy inspect' to check diff cleanliness; blocking findings are progress facts to fix before delivery.",
        ]
        status = "active"

    readback = {
        "anti_entropy_guidance": {
            "enabled": True,
            "applicable": True,
            "target_symbols": list(basis.target_symbols),
            "target_paths": list(basis.target_paths),
            "status": status,
        }
    }

    full_result = {
        "readback": readback,
        "context_fields": {"anti_entropy_context": context},
        "instructions": instructions,
        "result_fields": {},
    }

    encoded = json.dumps(full_result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > ANTI_ENTROPY_CONTEXT_MAX_RESULT_BYTES:
        # Bounded compaction: trim rationale or inspect tool descriptors if over budget
        compact_context = dict(context)
        compact_context.pop("inspect_tool", None)
        return {
            "readback": readback,
            "context_fields": {"anti_entropy_context": compact_context},
            "instructions": instructions,
            "result_fields": {},
        }

    return full_result


def anti_entropy_context_registration(
    payload: Mapping[str, Any],
    *,
    registry_path: Path,
    goal_id: str,
    agent_id: str,
    decision: Mapping[str, Any],
    turn_instance_id: str,
) -> TurnContextHookRegistration:
    """Create a TurnContextHookRegistration connecting anti-entropy into host input."""
    workspace = (
        Path(payload["workspace"]).resolve()
        if "workspace" in payload
        else (Path(payload["repo_path"]).resolve() if "repo_path" in payload else None)
    )

    def produce() -> Mapping[str, Any]:
        return anti_entropy_context_result(
            payload,
            goal_id=goal_id,
            decision=decision,
            workspace=workspace,
        )

    return TurnContextHookRegistration(
        hook_id="anti-entropy.turn-context",
        capability_id="anti-entropy-governance",
        readback_slots=("anti_entropy_guidance",),
        producer=produce,
        max_result_bytes=ANTI_ENTROPY_CONTEXT_MAX_RESULT_BYTES,
    )
