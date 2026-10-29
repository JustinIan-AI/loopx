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

_PY_DEF_OR_CLASS_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")

_TS_DEF_PATTERNS = [
    # function / async function / export function
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
    # class / abstract class / export class
    re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
    # interface
    re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
    # type alias: type Foo = ... or type Foo<T> = ...
    re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:<[^>]*>)?\s*="),
    # enum
    re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
    # const / let / var Foo = (...) => or = async (...) => (with optional generics)
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:<[^>]*>)?\s*(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
    ),
    # const / let / var Foo = function
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?function"
    ),
    # const / let / var Foo = (multiline signature start)
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:<[^>]*>)?\s*\("
    ),
]

_COMMENT_LINE_RE = re.compile(r"^\s*(?:#|//)\s*(.+)$")

_DOC_EXTENSIONS = {".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml"}
_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_PY_EXTENSIONS = {".py", ".pyi"}


def _hash_diff(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def find_repo_root(start_path: Path | None = None) -> Path:
    """Resolve repository root by looking for .git folder up the tree."""
    cur = (start_path or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return cur


def decode_git_diff_path(path_str: str, is_source: bool = True) -> str:
    """Decode a Git diff path with optional C-style quotes, stripping Git a/ or b/ prefix."""
    path_str = path_str.strip()
    if path_str.startswith('"') and path_str.endswith('"') and len(path_str) >= 2:
        inner = path_str[1:-1]
        try:
            raw_bytes = codecs.escape_decode(inner.encode("latin1"))[0]
            path_str = raw_bytes.decode("utf-8")
        except Exception:
            path_str = inner
    path_str = path_str.replace("\\", "/").strip()
    if path_str == "/dev/null":
        return "/dev/null"
    if is_source and path_str.startswith("a/"):
        return path_str[2:]
    elif not is_source and path_str.startswith("b/"):
        return path_str[2:]
    elif path_str.startswith("a/") or path_str.startswith("b/"):
        return path_str[2:]
    return path_str


def normalize_repo_path(path_str: str) -> str:
    """Normalize a repository-relative target path from RetirementBasis.

    Does not strip real directory segments (e.g. "a/") or leading hidden dots (e.g. ".api.py").
    """
    path_str = path_str.strip()
    if path_str.startswith('"') and path_str.endswith('"') and len(path_str) >= 2:
        inner = path_str[1:-1]
        try:
            raw_bytes = codecs.escape_decode(inner.encode("latin1"))[0]
            path_str = raw_bytes.decode("utf-8")
        except Exception:
            path_str = inner
    path_str = path_str.replace("\\", "/").strip()
    while path_str.startswith("./"):
        path_str = path_str[2:]
    while path_str.startswith("/"):
        path_str = path_str[1:]
    return path_str


def unquote_git_path(path_str: str) -> str:
    """Backward-compatible helper for unquoting git diff paths."""
    return decode_git_diff_path(path_str)


def parse_diff_git_header(line: str) -> tuple[str, str] | None:
    """Parse diff --git <src> <dst> line correctly, handling paths with spaces and quotes."""
    line = line.strip()
    if not line.startswith("diff --git "):
        return None
    rest = line[len("diff --git "):].strip()

    # Case 1: Both quoted, e.g. "a/foo bar" "b/foo bar"
    m_quoted = re.match(r'^("(?:[^"\\]|\\.)+")\s+("(?:[^"\\]|\\.)+")$', rest)
    if m_quoted:
        return m_quoted.group(1), m_quoted.group(2)

    # Case 2: Identical relative paths across a/ and b/ (deletion / modification)
    m_same = re.match(r"^a/(.+)\s+b/\1$", rest)
    if m_same:
        return f"a/{m_same.group(1)}", f"b/{m_same.group(1)}"

    # Case 3: Search for separator " b/" from the right
    if rest.startswith("a/"):
        idx = rest.rfind(" b/")
        if idx != -1:
            return rest[:idx], rest[idx + 1:]

    parts = rest.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _extract_deleted_paths(diff_text: str) -> set[str]:
    deleted_paths: set[str] = set()
    diff_text = textwrap.dedent(diff_text)

    # 1. Parse sections starting with diff --git
    diff_sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    for sec in diff_sections:
        if not sec.startswith("diff --git "):
            continue
        first_line = sec.splitlines()[0]
        is_deleted = bool(
            re.search(r"^deleted file mode \d+", sec, re.MULTILINE)
            or re.search(r"^\+\+\+\s+/dev/null", sec, re.MULTILINE)
        )
        if is_deleted:
            # Check for standard patch header: --- a/<path>\n+++ /dev/null
            m_patch = re.search(r"^---\s+(.+?)\n\+\+\+\s+/dev/null", sec, re.MULTILINE)
            if m_patch:
                clean_path = decode_git_diff_path(m_patch.group(1).strip(), is_source=True)
                if clean_path:
                    deleted_paths.add(clean_path)
                    continue

            # Empty file deletion with no content hunks (deleted file mode 100644 only)
            header_paths = parse_diff_git_header(first_line)
            if header_paths:
                clean_path = decode_git_diff_path(header_paths[0], is_source=True)
                if clean_path:
                    deleted_paths.add(clean_path)

    # 2. Also match standard --- <src>\n+++ /dev/null for diffs without diff --git
    for match in re.finditer(r"^---\s+(.+?)\n\+\+\+\s+/dev/null", diff_text, re.MULTILINE):
        raw_path = match.group(1).strip()
        clean_path = decode_git_diff_path(raw_path, is_source=True)
        if clean_path:
            deleted_paths.add(clean_path)

    return deleted_paths


def _extract_python_defs_from_code(code: str) -> set[str]:
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


def _extract_python_signature_defs(lines: list[str]) -> set[str]:
    defs: set[str] = set()
    for line in lines:
        m = _PY_DEF_OR_CLASS_RE.match(line)
        if m:
            name = m.group(1)
            try:
                tree = ast.parse(f"{line.strip()}\n    pass")
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name == name:
                            defs.add(name)
            except SyntaxError:
                defs.add(name)
    return defs


def _extract_ts_defs(lines: list[str]) -> set[str]:
    defs: set[str] = set()
    for line in lines:
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("//") or line_clean.startswith("/*") or line_clean.startswith("*"):
            continue
        for pattern in _TS_DEF_PATTERNS:
            m = pattern.match(line)
            if m:
                defs.add(m.group(1))
                break
    return defs


def _extract_defs_for_file(lines: list[str], file_path: str = "") -> set[str]:
    code_text = "\n".join(lines)
    ext = Path(file_path).suffix.lower() if file_path else ""

    if ext in _TS_EXTENSIONS:
        return _extract_ts_defs(lines)
    elif ext in _PY_EXTENSIONS:
        return _extract_python_defs_from_code(code_text) | _extract_python_signature_defs(lines)
    else:
        py_defs = _extract_python_defs_from_code(code_text) | _extract_python_signature_defs(lines)
        ts_defs = _extract_ts_defs(lines)
        return py_defs | ts_defs


def _analyze_diff_symbols(diff_text: str) -> tuple[set[str], set[str], set[str], set[str], list[str]]:
    """Analyze diff to extract deleted, reintroduced, surviving context, and disk defined symbols,

    plus all added lines for reference auditing.
    Returns:
        (deleted_symbols, reintroduced_symbols, surviving_context_defs, still_defined_on_disk, all_added_lines)
    """
    deleted_symbols: set[str] = set()
    reintroduced_symbols: set[str] = set()
    surviving_context_defs: set[str] = set()
    still_defined_on_disk: set[str] = set()
    all_added_lines: list[str] = []

    diff_text = textwrap.dedent(diff_text)
    file_diffs = re.split(r"(?=^diff --git |^--- )", diff_text, flags=re.MULTILINE)
    for f_diff in file_diffs:
        if not f_diff.strip():
            continue

        file_path = ""
        first_line = f_diff.splitlines()[0] if f_diff.startswith("diff --git ") else ""
        header_paths = parse_diff_git_header(first_line) if first_line else None
        if header_paths:
            file_path = decode_git_diff_path(header_paths[1], is_source=False)
        else:
            m_patch = re.search(r"^\+\+\+\s+(.+?)(?:\n|$)", f_diff, re.MULTILINE)
            if m_patch:
                file_path = decode_git_diff_path(m_patch.group(1).strip(), is_source=False)

        hunk_texts = re.split(r"(?=^@@\s+-\d+.*?\s+\+\d+.*?\s+@@)", f_diff, flags=re.MULTILINE)
        for h_text in hunk_texts:
            if not h_text.startswith("@@"):
                continue

            lines = h_text.splitlines()[1:]
            pre_lines: list[str] = []
            post_lines: list[str] = []
            deleted_lines: list[str] = []
            added_lines: list[str] = []
            context_lines: list[str] = []

            for line in lines:
                if line.startswith(" "):
                    clean_line = line[1:]
                    pre_lines.append(clean_line)
                    post_lines.append(clean_line)
                    context_lines.append(clean_line)
                elif line.startswith("-") and not line.startswith("---"):
                    clean_line = line[1:]
                    pre_lines.append(clean_line)
                    deleted_lines.append(clean_line)
                elif line.startswith("+") and not line.startswith("+++"):
                    clean_line = line[1:]
                    post_lines.append(clean_line)
                    added_lines.append(clean_line)
                    all_added_lines.append(clean_line)

            pre_defs = _extract_defs_for_file(pre_lines, file_path)
            post_defs = _extract_defs_for_file(post_lines, file_path)
            deleted_defs = _extract_defs_for_file(deleted_lines, file_path)
            added_defs = _extract_defs_for_file(added_lines, file_path)
            ctx_defs = _extract_defs_for_file(context_lines, file_path)

            for s in ctx_defs:
                surviving_context_defs.add(s)

            for s in pre_defs:
                if s not in post_defs:
                    deleted_symbols.add(s)

            for s in deleted_defs:
                if s not in post_defs and s not in added_defs:
                    deleted_symbols.add(s)

            for s in added_defs:
                reintroduced_symbols.add(s)

        is_file_deleted = bool(
            re.search(r"^deleted file mode \d+", f_diff, re.MULTILINE)
            or re.search(r"^\+\+\+\s+/dev/null", f_diff, re.MULTILINE)
        )
        if file_path and file_path != "/dev/null" and not is_file_deleted:
            repo_root = find_repo_root()
            disk_p = (repo_root / file_path).resolve()
            if not disk_p.is_file():
                disk_p = Path(file_path).resolve()
            if disk_p.is_file():
                try:
                    content = disk_p.read_text(encoding="utf-8")
                    disk_defs = _extract_defs_for_file(content.splitlines(), file_path)
                    for s in disk_defs:
                        still_defined_on_disk.add(s)
                        if s in deleted_symbols:
                            deleted_symbols.remove(s)
                except Exception:
                    pass

    return deleted_symbols, reintroduced_symbols, surviving_context_defs, still_defined_on_disk, all_added_lines


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

    deleted_symbols, reintroduced_symbols, surviving_context_defs, still_defined_on_disk, all_added_lines = _analyze_diff_symbols(diff_text)
    deleted_paths = _extract_deleted_paths(diff_text)

    for symbol in basis.target_symbols:
        # Check if the symbol is reintroduced as definition OR actively referenced/called in added code
        is_reintroduced = symbol in reintroduced_symbols
        if not is_reintroduced:
            sym_pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
            for added_l in all_added_lines:
                clean_added = added_l.strip()
                if clean_added.startswith(("#", "//", "/*", "*")):
                    continue
                if sym_pattern.search(clean_added):
                    is_reintroduced = True
                    break

        if is_reintroduced:
            findings.append(
                AntiEntropyFinding(
                    rule_id="AE002-ZOMBIE_SYMBOL_REINTRODUCED",
                    severity="blocking",
                    message=(
                        f"Target symbol '{symbol}' was slated for retirement but reintroduced "
                        "as an active definition or called in added lines in the diff or repository."
                    ),
                    location=symbol,
                )
            )
        elif symbol not in deleted_symbols or symbol in surviving_context_defs or symbol in still_defined_on_disk:
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
        norm_path = normalize_repo_path(path)
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

        ext = Path(file_path).suffix.lower() if file_path else ""
        if ext in _DOC_EXTENSIONS:
            continue

        consecutive_commented_code = 0
        start_line = ""

        for line in f_diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                consecutive_commented_code = 0
                continue

            raw = line[1:].strip()
            comment_match = _COMMENT_LINE_RE.match(raw)
            if comment_match:
                comment_content = comment_match.group(1).strip()
                is_code_stmt = False

                if _PY_DEF_OR_CLASS_RE.match(comment_content):
                    is_code_stmt = True
                elif any(p.match(comment_content) for p in _TS_DEF_PATTERNS):
                    is_code_stmt = True
                elif comment_content.endswith(";") or comment_content.startswith(("import ", "from ", "export ", "return ", "if ", "for ")):
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
            deleted_symbols, reintroduced_symbols, surviving_context_defs, still_defined_on_disk, all_added_lines = _analyze_diff_symbols(diff_text)
            failing_symbols = {
                f.location for f in all_findings
                if f.location and f.rule_id in ("AE001-DELETE_FIRST_VIOLATION", "AE002-ZOMBIE_SYMBOL_REINTRODUCED")
            }
            for s in basis.target_symbols:
                sym_pattern = re.compile(r"\b" + re.escape(s) + r"\b")
                has_added_ref = any(
                    sym_pattern.search(l) for l in all_added_lines
                    if not l.strip().startswith(("#", "//", "/*", "*"))
                )
                if (
                    s in deleted_symbols
                    and s not in reintroduced_symbols
                    and not has_added_ref
                    and s not in surviving_context_defs
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
