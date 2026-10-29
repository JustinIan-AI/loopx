# Architecture Decision Record: Anti-Entropy Governance Benchmark & Evaluation Harness

- **Status**: Accepted (Design Grill Complete)
- **Date**: 2026-10-29
- **Scope**: Out-of-band evaluation harness for quantifying anti-entropy (Aegis delete-first) impact
- **Target Constraint**: 100% zero-intrusion to LoopX core control plane (`loopx/control_plane/`), default-off opt-in capability

---

## 1. Context & Problem Statement

When autonomous coding agents execute refactoring, feature replacement, or protocol migration tasks, they suffer from systematic "entropy growth":
1. **Fallback/Wrapper Addiction**: Instead of cleanly deleting old classes/functions, agents keep them as backwards-compatibility shims or wrap them inside new logic.
2. **Ghost Code Residue**: Agents frequently comment out superseded blocks (`# old_logic()`) rather than deleting them.
3. **Zombie Reintroduction**: When tests fail, agents often recreate the deprecated symbol in the newly modified files.

LoopX possesses an existing deterministic AST-based anti-entropy engine (`loopx/capabilities/anti_entropy/`). The core question was:
- Should test/benchmark scenarios overlay a full real-world development task?
- How should the anti-entropy capability be injected into commands to measure the delta between having vs. not having anti-entropy governance?
- What task scenario is most appropriate to evaluate this without compromising LoopX core stability?

---

## 2. Decision Points & Trade-Off Analysis

### Decision 1: Loading Mechanism & Interception Timing
- **Considered Alternatives**:
  - *Option 1*: Pure prompt guidance (soft constraint). Zero tooling overhead, but models consistently regress under multi-file pressure.
  - *Option 2*: Post-hoc terminal gate only. Verifies diff at submission; fails late when the agent has already exhausted budget, leading to blind rollback loops.
  - *Option 3 (Selected)*: **Two-Tier Closed Loop (Execution Tool + Submission Gate)**.
    - Exposes `anti_entropy_inspect` as an advisory tool during execution so the agent can self-audit diff cleanliness.
    - Chains `loopx anti-entropy verify` into the delivery evaluation command, failing the run if `AntiEntropyReceipt.verdict != "passed"`.
- **Trade-off**: Increases tool-call rounds and slight token overhead per run, in exchange for deterministic first-pass cleanliness and eliminating expensive blind rollbacks.

---

### Decision 2: Zero-Intrusion Packaging & Retirement Basis Injection
- **Considered Alternatives**:
  - *Option 1*: Hardwire anti-entropy check into `loopx/control_plane/turn_driver/`. Causes direct intrusion into generic mainline scheduling, violating domain-neutrality.
  - *Option 2*: Dynamic runtime AST inference without declaration. Prone to false positives when agents refactor internal helper functions.
  - *Option 3 (Selected)*: **Declarative Sidecar / Task-Envelope Hook Pattern**.
    - The task specification explicitly provides an `anti_entropy_basis` (target symbols and target paths to retire).
    - The benchmark/test runner wraps the task externally: conditionally injects the inspect tool into the agent envelope and appends the verify CLI command to the test command.
    - `loopx/control_plane/` remains completely unmodified (0-line diff).
- **Trade-off**: Requires test cases to declare their target retirement list in advance, rather than inferring intent magically.

---

### Decision 3: Evaluation Benchmark Scenario (Testbed Task) Selection
- **Considered Alternatives**:
  - *Option 1*: In-repo dogfooding on live LoopX modules. High risk of breaking the developer active branch during failed baseline runs; violates mainline safety.
  - *Option 2*: External SWE-bench Docker benchmark. Requires heavyweight containers and environment setups; feedback loop too slow for local iterative testing.
  - *Option 3 (Selected)*: **Self-Contained Realistic Engineering Playground in `examples/anti_entropy/`**.
    - A multi-module Python service (protocol parser + streaming receiver + client caller + unit test suite).
    - Real task: Migrate all callers from `LegacyProtocolParser` to `StreamMessageParser` and physically delete `legacy_parser.py`.
    - Automated comparator evaluates Arm 1 (Baseline), Arm 2 (Prompt Only), and Arm 3 (Two-Tier Anti-Entropy).
- **Trade-off**: Tests on a standalone engineering playground rather than mutating production LoopX code directly.

---

## 3. First-Principles Minimality Justification (Aegis Integration)

1. **Change Necessity**: Reuses the existing `loopx/capabilities/anti_entropy/` AST engine without creating redundant verification abstractions.
2. **Fallback Ceiling**: Refuses to add fallback adapters; enforces physical deletion of deprecated symbols (`/dev/null` diff + AST symbol absence).
3. **Delete-First & Anti-Entropy**: The evaluation suite directly measures net negative lines of code, absence of zombie wrappers, and 0 ghost code blocks.
4. **Mainline Purity**: All new harness scripts live in `examples/anti_entropy/` and `docs/decisions/`. No files under `loopx/control_plane/` are touched.

---

## 4. Impacted System Components & Retired Paths

- **Impacted Components**:
  - `docs/decisions/anti-entropy-benchmark-decisions.md`: Canonical ADR record.
  - `examples/anti_entropy/case_study/`: Standalone realistic multi-file testbed.
  - `examples/anti_entropy/compare_arms.py`: Out-of-band evaluation comparator for measuring differential.
- **Retired / Disallowed Paths**:
  - Disallowed: Modifying `loopx/control_plane/turn_driver/executor.py` for benchmark gates.
  - Disallowed: Ad-hoc heuristic AST diff guesswork without a declared basis.
