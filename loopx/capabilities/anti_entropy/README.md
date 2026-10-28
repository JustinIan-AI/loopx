# Anti-Entropy & Code Retirement Governance

Anti-Entropy Governance provides LoopX with an automated, deterministic
code-retirement and diff-hygiene verification contract. It is **default-off**
and designed to support agent-driven refactoring and feature decommissioning workflows.

## Overview

When autonomous agents perform refactoring or feature replacement tasks, they
frequently exhibit an "entropy growth" anti-pattern: layering yet another wrapper
or fallback branch without safely deleting superseded implementations. Over
long-horizon tasks, this results in accumulated ghost code, redundant complexity,
and bloated execution contexts.

The Anti-Entropy capability addresses this through:

1. **AST-Based Delete-First Enforcement (`verify_delete_first`)**:
   Uses Python's syntax tree parser to verify that declared retirement targets
   (functions and classes) are genuinely removed from the codebase rather than
   merely bypassed or wrapped.
2. **Zombie Symbol Prevention**:
   Detects if a symbol marked for retirement is reintroduced as a new definition
   in newly added code lines.
3. **Target Paths Verification**:
   Ensures that declared legacy file paths slated for deletion are completely
   removed in the unified diff (`/dev/null`).
4. **Ghost Code Auditing (`audit_ghost_code`)**:
   Identifies commented-out code blocks left behind, surfacing them as advisory
   quality findings without blocking legitimate documentation or explanatory comments.
5. **Durable Receipt Generation (`AntiEntropyReceipt`)**:
   Runs deterministic hygiene oracles against the exact git diff and produces
   an immutable receipt recording verified retired symbols and findings.

## Attribution & Acknowledgments

This capability adopts and operationalizes the core engineering principles of
**delete-first retirement**, **ghost code prevention**, and **minimality verification**
inspired by the **Aegis** engineering methodology.

## Usage

Inspect the capability and its registered protocols:

```bash
loopx capability show anti-entropy-governance --format json
```

Verify diff hygiene against a declared retirement basis:

```bash
loopx anti-entropy verify --goal-id <goal-id> --diff-file <diff.patch> --basis-json <basis.json> --format json
```

Example `basis.json`:

```json
{
  "target_symbols": ["old_deprecated_calculator"],
  "target_paths": ["legacy/old_module.py"],
  "allow_compat_seam": false,
  "rationale": "Superseded by modern_calculator implementation"
}
```

## Boundaries

- The capability is default-off and operates as an opt-in code hygiene verifier via CLI and typed receipts.
- Delete-first requires target old logic to be cleanly decommissioned in the diff without re-introducing wrappers or zombie symbols.
- AST-based diff hygiene verifies exact code diffs without mutating repository code or performing speculative git writes.
- Target paths declared for decommissioning must be cleanly removed in the unified diff.
- Advisory findings highlight non-blocking hygiene opportunities (such as dead commented code) without causing false positive rejections of documentation or explanations.
