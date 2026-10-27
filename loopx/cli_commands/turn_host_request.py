"""Parse the existing CLI Host request before any Turn execution.

This is CLI argument transport and host-adapter binding, not a decision owner.
The Turn driver retains admission, effects and settlement.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast


def prepare_turn_host_request(
    args: argparse.Namespace, payload: Mapping[str, Any], *,
    native_host_runner: Callable[..., dict[str, Any]] | None,
    native_session_binding_resolver: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
    native_task_validator: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None,
    validator_builder: Callable[..., Callable[..., Mapping[str, Any]]],
) -> tuple[Path, list[str] | None, Callable[..., Mapping[str, Any]] | None, dict[str, Any]]:
    project = Path(args.project).expanduser().resolve()
    planned_host = (
        cast(dict[str, Any], payload.get("host")) if isinstance(payload.get("host"), dict) else {}
    )
    if planned_host.get("kind") != args.host:
        raise ValueError("--host must match the journaled LoopX Turn plan")
    if args.host == "codex-app-server" and (
        native_host_runner is None or native_session_binding_resolver is None
    ):
        raise ValueError("app-server execution requires its original in-process Chat connection")
    if args.host != "codex-app-server" and (
        native_host_runner is not None or native_session_binding_resolver is not None
    ):
        raise ValueError("a native Chat connection cannot replace a selected CLI host")
    if args.host == "generic-cli":
        if not args.host_command_json:
            raise ValueError(
                "generic-cli requires --host-adapter-command-json "
                "(alias --host-command-json)"
            )
        raw_argv = json.loads(args.host_command_json)
        if not isinstance(raw_argv, list) or not all(
            isinstance(item, str) for item in raw_argv
        ):
            raise ValueError(
                "--host-adapter-command-json must be a JSON string array"
            )
    else:
        if args.host_command_json:
            raise ValueError(f"{args.host} does not accept --host-command-json")
        raw_argv = None
    if native_task_validator is not None and args.host != "codex-app-server":
        raise ValueError("a native task validator cannot replace a selected CLI validator")
    task_validator = native_task_validator
    if args.validation_command_json:
        raw_validation_argv = json.loads(args.validation_command_json)
        if not isinstance(raw_validation_argv, list) or not all(
            isinstance(item, str) for item in raw_validation_argv
        ):
            raise ValueError(
                "--validation-command-json must be a JSON string array"
            )
        if task_validator is None:
            task_validator = validator_builder(
                raw_validation_argv,
                project=project,
                timeout_seconds=args.validation_timeout_seconds,
                failure_recovery_kind=args.validation_failure_kind,
            )
    envelope = (
        cast(dict[str, Any], payload.get("turn_envelope"))
        if isinstance(payload.get("turn_envelope"), dict)
        else {}
    )
    return project, raw_argv, task_validator, envelope
