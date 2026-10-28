from __future__ import annotations

import ast
import codecs
import hashlib
import re
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contract import (
    AntiEntropyFinding,
    AntiEntropyReceipt,
    RetirementBasis,
)

_DEF_OR_CLASS_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_COMMENT_LINE_RE = re.compile(r"^\s*(?:#|//)\s*(.+)$")


def _hash_diff(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def unquote_git_path(path_str: str) -> str:
    """Unquote and normalize Git C-style quoted paths with octal or hex escapes."""
    path_str = path_str.strip()
    if path_str.startswith('"') and path_str.endswith('"') and len(path_str) >= 2:
        inner = path_str[1:-1]
        try:
            raw_bytes = codecs.escape_decode(inner.encode("latin1"))[0]
            path_str = raw_bytes.decode("utf-8")
        except Exception:
            path_str = inner
    path_str = path_str.replace("\\", "/").strip()
    if path_str.startswith("a/") or path_str.startswith("b/"):
        path_str = path_str[2:]
    return path_str.lstrip("./").lstrip("/")


def _extract_deleted_paths(diff_text: str) -> set[str]:
    deleted_paths: set[str] = set()
    diff_text = textwrap.dedent(diff_text)

    # 1. Parse sections starting with diff --git
    diff_sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    for sec in diff_sections:
        if not sec.startswith("diff --git "):
            continue
        header_match = re.match(r"^diff --git\s+(.+?)\s+(.+?)(?:\n|$)", sec)
        if not header_match:
            continue
        src_raw = header_match.group(1).strip()
        is_deleted = bool(
            re.search(r"^deleted file mode \d+", sec, re.MULTILINE)
            or re.search(r"^\+\+\+\s+/dev/null", sec, re.MULTILINE)
        )
        if is_deleted:
            clean_path = unquote_git_path(src_raw)
            if clean_path:
                deleted_paths.add(clean_path)

    # 2. Also match standard --- <src>\n+++ /dev/null for diffs without diff --git
    for match in re.finditer(r"^---\s+(.+?)\n\+\+\+\s+/dev/null", diff_text, re.MULTILINE):
        raw_path = match.group(1).strip()
        clean_path = unquote_git_path(raw_path)
        if clean_path:
            deleted_paths.add(clean_path)

    return deleted_paths


def _extract_defs_from_code(code: str) -> set[str]:
    if not code.strip():
        return set()
    dedented = textwrap.dedent(code)
    try:
        tree = ast.parse(dedented)
        return {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
    except SyntaxError:
        try:
            wrapped = "def _wrapper_():\n" + textwrap.indent(dedented, "    ")
            tree = ast.parse(wrapped)
            return {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name != "_wrapper_"
            }
        except SyntaxError:
            return set()


def _extract_signature_defs(lines: list[str]) -> set[str]:
    defs: set[str] = set()
    for line in lines:
        m = _DEF_OR_CLASS_RE.match(line)
        if m:
            name = m.group(1)
            try:
                tree = ast.parse(f"{line.strip()}\n    pass")
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name == name:
                            defs.add(name)
            except SyntaxError:
                pass
    return defs


def _analyze_diff_symbols(diff_text: str) -> tuple[set[str], set[str], set[str]]:
    deleted_symbols: set[str] = set()
    reintroduced_symbols: set[str] = set()
    still_defined_on_disk: set[str] = set()

    diff_text = textwrap.dedent(diff_text)
    file_diffs = re.split(r"(?=^diff --git |^--- )", diff_text, flags=re.MULTILINE)
    for f_diff in file_diffs:
        if not f_diff.strip():
            continue

        file_path = ""
        m_git = re.search(r"^diff --git\s+(.+?)\s+(.+?)(?:\n|$)", f_diff, re.MULTILINE)
        if m_git:
            file_path = unquote_git_path(m_git.group(2).strip())
        else:
            m_patch = re.search(r"^\+\+\+\s+(.+?)(?:\n|$)", f_diff, re.MULTILINE)
            if m_patch:
                file_path = unquote_git_path(m_patch.group(1).strip())

        hunk_texts = re.split(r"(?=^@@\s+-\d+.*?\s+\+\d+.*?\s+@@)", f_diff, flags=re.MULTILINE)
        for h_text in hunk_texts:
            if not h_text.startswith("@@"):
                continue

            lines = h_text.splitlines()[1:]
            pre_lines: list[str] = []
            post_lines: list[str] = []
            deleted_lines: list[str] = []
            added_lines: list[str] = []

            for line in lines:
                if line.startswith(" "):
                    pre_lines.append(line[1:])
                    post_lines.append(line[1:])
                elif line.startswith("-") and not line.startswith("---"):
                    pre_lines.append(line[1:])
                    deleted_lines.append(line[1:])
                elif line.startswith("+") and not line.startswith("+++"):
                    post_lines.append(line[1:])
                    added_lines.append(line[1:])

            pre_defs = _extract_defs_from_code("\n".join(pre_lines))
            post_defs = _extract_defs_from_code("\n".join(post_lines))
            added_defs = _extract_defs_from_code("\n".join(added_lines)) | _extract_signature_defs(added_lines)
            deleted_defs = _extract_defs_from_code("\n".join(deleted_lines)) | _extract_signature_defs(deleted_lines)

            for s in pre_defs:
                if s not in post_defs:
                    deleted_symbols.add(s)

            for s in deleted_defs:
                if s not in post_defs and s not in added_defs:
                    deleted_symbols.add(s)

            for s in added_defs:
                reintroduced_symbols.add(s)
            for s in post_defs:
                if s in added_defs or s in deleted_defs:
                    reintroduced_symbols.add(s)

        if file_path and file_path != "/dev/null":
            disk_p = Path(file_path)
            if disk_p.is_file():
                try:
                    content = disk_p.read_text(encoding="utf-8")
                    disk_defs = _extract_defs_from_code(content)
                    for s in disk_defs:
                        still_defined_on_disk.add(s)
                        if s in deleted_symbols:
                            deleted_symbols.remove(s)
                except Exception:
                    pass

    return deleted_symbols, reintroduced_symbols, still_defined_on_disk


def verify_delete_first(
    diff_text: str,
    retirement_basis: RetirementBasis | Mapping[str, Any] | None = None,
) -> list[AntiEntropyFinding]:
    findings: list[AntiEntropyFinding] = []

    if retirement_basis is None:
        return findings

    basis = (
        retirement_basis
        if isinstance(retirement_basis, RetirementBasis)
        else RetirementBasis.from_dict(retirement_basis)
    )

    if not basis.target_symbols and not basis.target_paths:
        return findings

    diff_text = textwrap.dedent(diff_text)
    if not diff_text.strip():
        for symbol in basis.target_symbols:
            findings.append(
                AntiEntropyFinding(
                    rule_id="AE001-DELETE_FIRST_VIOLATION",
                    severity="blocking" if not basis.allow_compat_seam else "advisory",
                    message=(
                        f"Target symbol '{symbol}' was declared for retirement but diff is empty. "
                        "Delete-first requires explicit removal of superseded logic."
                    ),
                    location=symbol,
                )
            )
        for path in basis.target_paths:
            findings.append(
                AntiEntropyFinding(
                    rule_id="AE001-DELETE_FIRST_VIOLATION",
                    severity="blocking" if not basis.allow_compat_seam else "advisory",
                    message=(
                        f"Target path '{path}' was declared for retirement but diff is empty."
                    ),
                    location=path,
                )
            )
        return findings

    deleted_symbols, reintroduced_symbols, still_defined_on_disk = _analyze_diff_symbols(diff_text)
    deleted_paths = _extract_deleted_paths(diff_text)

    for symbol in basis.target_symbols:
        if symbol in reintroduced_symbols or symbol in still_defined_on_disk:
            findings.append(
                AntiEntropyFinding(
                    rule_id="AE002-ZOMBIE_SYMBOL_REINTRODUCED",
                    severity="blocking",
                    message=(
                        f"Target symbol '{symbol}' was slated for retirement but reintroduced "
                        "or retained as an active definition in the diff or repository."
                    ),
                    location=symbol,
                )
            )
        elif symbol not in deleted_symbols:
            findings.append(
                AntiEntropyFinding(
                    rule_id="AE001-DELETE_FIRST_VIOLATION",
                    severity="blocking" if not basis.allow_compat_seam else "advisory",
                    message=(
                        f"Target symbol '{symbol}' was declared for retirement in RetirementBasis "
                        "but its definition was not removed from the diff. Delete-first requires deleting "
                        "superseded definitions rather than merely removing calls, altering docs, or adding wrappers."
                    ),
                    location=symbol,
                )
            )

    for path in basis.target_paths:
        norm_path = unquote_git_path(path)
        if norm_path not in deleted_paths:
            findings.append(
                AntiEntropyFinding(
                    rule_id="AE001-DELETE_FIRST_VIOLATION",
                    severity="blocking" if not basis.allow_compat_seam else "advisory",
                    message=(
                        f"Target path '{path}' was declared for retirement in RetirementBasis "
                        "but was not removed in the diff."
                    ),
                    location=path,
                )
            )

    return findings


def audit_ghost_code(diff_text: str) -> list[AntiEntropyFinding]:
    findings: list[AntiEntropyFinding] = []
    consecutive_commented_code = 0
    start_line = ""

    diff_text = textwrap.dedent(diff_text)
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            consecutive_commented_code = 0
            continue

        raw = line[1:].strip()
        comment_match = _COMMENT_LINE_RE.match(raw)
        if comment_match:
            comment_content = comment_match.group(1).strip()
            is_code_stmt = False
            if _DEF_OR_CLASS_RE.match(comment_content):
                is_code_stmt = True
            else:
                try:
                    ast.parse(comment_content)
                    is_code_stmt = True
                except SyntaxError:
                    is_code_stmt = False

            if is_code_stmt:
                consecutive_commented_code += 1
                if consecutive_commented_code == 1:
                    start_line = raw
            else:
                consecutive_commented_code = 0
        else:
            consecutive_commented_code = 0

        if consecutive_commented_code >= 3:
            findings.append(
                AntiEntropyFinding(
                    rule_id="AE003-COMMENTED_CODE_ADVISORY",
                    severity="advisory",
                    message=(
                        "Detected 3+ consecutive lines of commented-out code. "
                        "Consider complete removal under version control rather than ghost commenting."
                    ),
                    location=start_line[:80],
                )
            )
            consecutive_commented_code = 0

    return findings


def evaluate_diff_hygiene(
    diff_text: str,
    retirement_basis: RetirementBasis | Mapping[str, Any] | None = None,
) -> AntiEntropyReceipt:
    diff_text = textwrap.dedent(diff_text)
    diff_hash = _hash_diff(diff_text)
    all_findings: list[AntiEntropyFinding] = []

    all_findings.extend(verify_delete_first(diff_text, retirement_basis))
    all_findings.extend(audit_ghost_code(diff_text))

    has_blocking = any(f.severity == "blocking" for f in all_findings)
    verdict = "rejected" if has_blocking else "passed"

    retired: list[str] = []
    if retirement_basis:
        basis = (
            retirement_basis
            if isinstance(retirement_basis, RetirementBasis)
            else RetirementBasis.from_dict(retirement_basis)
        )
        if basis.target_symbols and diff_text.strip():
            deleted_symbols, reintroduced_symbols, still_defined_on_disk = _analyze_diff_symbols(diff_text)
            failing_symbols = {
                f.location for f in all_findings
                if f.location and f.rule_id in ("AE001-DELETE_FIRST_VIOLATION", "AE002-ZOMBIE_SYMBOL_REINTRODUCED")
            }
            for s in basis.target_symbols:
                if (
                    s in deleted_symbols
                    and s not in reintroduced_symbols
                    and s not in still_defined_on_disk
                    and s not in failing_symbols
                ):
                    retired.append(s)

    return AntiEntropyReceipt(
        diff_hash=diff_hash,
        verdict=verdict,
        findings=tuple(all_findings),
        retired_symbols=tuple(sorted(retired)),
    )
