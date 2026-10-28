from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..capabilities.anti_entropy.cli import run_anti_entropy_verify
from ..capabilities.anti_entropy.contract import ANTI_ENTROPY_CAPABILITY_ID

PrintPayload = Callable[
    [dict[str, Any], str, Callable[[dict[str, Any]], str]], str | None
]
FormatSelector = Callable[..., str]
AddFormat = Callable[[argparse.ArgumentParser], None]


def register_anti_entropy_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    add_subcommand_format: AddFormat,
) -> None:
    parser = subparsers.add_parser(
        "anti-entropy",
        help="Verify diff hygiene and delete-first code retirement governance.",
    )
    commands = parser.add_subparsers(
        dest="anti_entropy_command", required=True
    )
    verify = commands.add_parser(
        "verify",
        help="Verify diff against a declared retirement basis and audit diff hygiene.",
    )
    add_subcommand_format(verify)
    verify.add_argument("--goal-id", help="Associated LoopX goal identifier.")
    verify.add_argument(
        "--diff-file",
        help="Path to unified diff patch file, or - for stdin.",
    )
    verify.add_argument(
        "--basis-json",
        help="JSON string or file path containing RetirementBasis specification.",
    )


def _render(payload: dict[str, Any]) -> str:
    lines = [f"# Anti-Entropy Governance ({payload.get('command')})", ""]
    lines.append(f"- verdict: `{payload.get('verdict')}`")
    lines.append(f"- ok: `{payload.get('ok')}`")
    if payload.get("goal_id"):
        lines.append(f"- goal_id: `{payload.get('goal_id')}`")
    lines.append(f"- finding_count: `{payload.get('finding_count', 0)}`")
    lines.append(f"- blocking_finding_count: `{payload.get('blocking_finding_count', 0)}`")
    lines.append(f"- advisory_finding_count: `{payload.get('advisory_finding_count', 0)}`")

    receipt = payload.get("receipt", {})
    if receipt:
        lines.append("")
        lines.append("## Receipt")
        lines.append(f"- diff_hash: `{receipt.get('diff_hash')}`")
        retired = receipt.get("retired_symbols", [])
        if retired:
            lines.append(f"- retired_symbols: `{', '.join(retired)}`")
        findings = receipt.get("findings", [])
        if findings:
            lines.append("")
            lines.append("## Findings")
            for item in findings:
                sev = item.get("severity", "").upper()
                rule = item.get("rule_id")
                msg = item.get("message")
                lines.append(f"- [{sev}] {rule}: {msg}")
    return "\n".join(lines) + "\n"


def handle_anti_entropy_command(
    args: argparse.Namespace,
    *,
    registry_path: Path,
    runtime_root_arg: str | None,
    output_format: FormatSelector,
    print_payload: PrintPayload,
) -> int | None:
    if args.command != "anti-entropy":
        return None
    try:
        command = args.anti_entropy_command
        if command == "verify":
            diff_text = ""
            if args.diff_file == "-":
                diff_text = sys.stdin.read()
            elif args.diff_file:
                diff_path = Path(args.diff_file).expanduser()
                if diff_path.is_file():
                    diff_text = diff_path.read_text(encoding="utf-8")
                else:
                    raise ValueError(f"diff file not found: {args.diff_file}")

            basis_data = None
            if args.basis_json is not None:
                raw_basis = args.basis_json.strip()
                if raw_basis.startswith("{") or raw_basis.startswith("["):
                    basis_data = json.loads(raw_basis)
                else:
                    path = Path(raw_basis).expanduser()
                    if path.is_file():
                        basis_data = json.loads(path.read_text(encoding="utf-8"))
                    else:
                        try:
                            basis_data = json.loads(raw_basis)
                        except Exception:
                            raise ValueError(f"basis file not found: {raw_basis}")

            payload = run_anti_entropy_verify(
                diff_text=diff_text,
                basis_data=basis_data,
                goal_id=args.goal_id,
            )
        else:
            raise ValueError(f"unknown anti-entropy command `{command}`")
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {ANTI_ENTROPY_CAPABILITY_ID}: {exc}", file=sys.stderr)
        return 2

    print_payload(payload, output_format(args), _render)
    return 0 if payload.get("ok") else 1
