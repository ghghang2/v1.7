# Academic & Preprint Research: Multi-Agent Frameworks & Harnesses (2025-2026)

**Scope:** Exhaustive survey of the LATEST preprints + research publications (arXiv,
Hugging Face Papers, Papers with Code, AlphaXiv, papers.cool, GitHub trending + star
cross-reference) plus major research-lab public output, focused on multi-agent LLM
frameworks, agent harnesses, and agent orchestration. All sources surveyed
2026-09-11.

**Sources & method:**
- arXiv API: 17 date-sorted queries, 510 hits / 408 unique, last ~3 months
  (res-arxiv: `/tmp/nbchat_academic_research/arxiv.md`).
- Online repositories + research aggregators: Hugging Face Daily/Trending Papers,
  Papers with Code, GitHub trending (Python), arXiv cs.MA + cs.AI recent, AlphaXiv,
  papers.cool + known-framework star cross-reference (res-repos:
  `/tmp/nbchat_academic_research/repos.md`).
- My own targeted arXiv reads (observability, self-evolving execution, test-time
  graph engineering, self-testing/self-judging, constraint tracking, skill
  consolidation, verification, memory, tree search, deterministic guardrails).

---

## The big picture (2025 -> 2026)

The center of gravity moved from **frameworks** (2024: MetaGPT/AutoGen/CrewAI
chat-style role play) to **harnesses** (2025-2026: Deer-Flow "SuperAgent harness",
Prime Agent self-improving RLM harness, Show-Harness, T1-style terminal RL, and a
fast-forming **Agent Skills ecosystem**). With frontier models commoditizing, the
differentiator is the *scaffolding*: REPLs, sandboxes, verifiers, memory, and skill
registries.

## Recurring themes (convergent across sources)

1. **The harness is the product + the optimization target.** A large 2026 cluster
   trains/evolves/compiles the harness (prompts, tools, loops, context management)
   while freezing model weights (Ecdysis, Harness-of-Harness, StarHarness,
   Evo-Harness, Credo, Hierarchical Self-Improvement). Theory now bounds how far
   self-modification can safely go (Safe Harness Self-Evolution).
2. **Subagent orchestration is the standard production architecture** and is finally
   being benchmarked (ClawArena-Team), with open questions on subagents-vs-skills
   (Subagents vs Agent Skills) and team interchangeability (Testing
   Interchangeability: they are NOT interchangeable - pin/validate roles).
3. **Orchestration shifts from static topologies to adaptive, evidence-driven
   control:** inference-time workflow graphs (ReActNet), trace-grounded replanning
   (TROVE), progress-guided routing (ProgRouter), market-based allocation (Markets,
   Not Planners). Commit less up front; observe progress; reroute/kill on evidence.
4. **Long-horizon reliability is THE open problem.** Agents "rot" past benchmark
   horizons (How Fast Do Agents Rot); self-reported progress is untrustworthy (The
   Unreliable Progress Bar); memory management is becoming a learned process
   (Memory as a Controlled Process, Weighted Memory Tree, MEMO); live
   self-improvement during the run (PILOT in the Loop).
5. **Harness-level security is an emerging attack surface** for coding agents:
   prompt injection, ambient tool authority, harness-side privilege escalation via
   context construction (When Context Gets Root), cross-substrate authority gaps
   (Beyond Agent Harnesses), skill misevolution (Practice Makes Unsafe).
   Countermeasures are harness-native: capability-scoped authority
   (Authority Is Not a String / CapScope), workflow-level anomaly detection (Skynet),
   skill supply-chain scanning (NVIDIA/SkillSpector).
6. **Verifier-gated, minimally-social agent teams beat big role teams.** Structured
   disagreement with an objective verifier (Adversarial Review, ArcticSwarm,
   Bilevel Coordinated Reflection) + deterministic guardrails the judge cannot
   override (LLM-as-a-Judge Is Not an Oracle).
7. **Test-time compute via external state (the RLM pattern):** a persistent REPL /
   external computation as the agent's test-time compute (Prime Agent/RLM, T1).
8. **The Agent Skills economy is the fastest-moving part of the field:**
   openai/skills + anthropics/skills as canonical formats; consumer skills gaining
   1k-10k stars/week; NVIDIA/SkillSpector bootstrapping skill security scanning;
   COBRA-Skills optimizing skill libraries with bandits.

## The single flagship result (for a TERMINAL coding agent)

**T1: Terminal Agent Reinforcement Learning for Long-Horizon Tasks**
(AlphaXiv:2609.11042) - a 122B MoE trained with RL to operate a *real shell* in a
cloud sandbox for 300+ tool-call turns, **rewarded by executing each task's own
verifier** (tests/build/lint as dense process rewards). This is the field's most
concrete "terminal coding agent" result, and its recipe (live verifier-driven
process score) is the single highest-leverage reliability feature for nbchat.

---

## TOP CANDIDATES for nbchat.tui3 (ranked by convergence + leverage + practicality)

Cross-referenced across res-arxiv, res-repos, and my own arXiv reads. Each is
actionable as a SAFE, ADDITIVE, TESTABLE slice (no changes to the v1 REPL or the
tui2 base).

### Candidate A — Verifier-driven process score ("the T1 recipe")
**What:** Wire each task's executable verifiers (tests, build, lint, typecheck) as
a continuous process signal: a live per-task "verifier score" + a progress bar that
updates as the agent works, driven by *measured* outcomes (tests passing, files
changed) - not self-reported status.
**Why:** T1 (flagship terminal-agent result), Proof-Carrying Cognition (the
verification gap; verifier-gold correlation = the test-time-compute exchange rate),
LLM-as-a-Judge Is Not an Oracle (demote the LLM judge to advisor; gate on
deterministic verification), The Unreliable Progress Bar (use objective signals for
progress). The single highest-leverage reliability + trust feature.
**Slice:** a `/verify` command + a live process-score pill in the status line,
computed from the most recent test/build/lint tool results.

### Candidate B — Objective long-run "rot monitor" / health
**What:** Track objective health of a long session - time since last verifiable
progress, test-suite trend, context bloat, drift from the original plan - and act:
compact, checkpoint + re-plan, or warn.
**Why:** How Fast Do Agents Rot (agents degrade sharply past benchmark horizons),
The Unreliable Progress Bar (self-reported progress is untrustworthy), PILOT in the
Loop (live self-improvement during the run).
**Slice:** a `/health` command + a rot indicator (a health pill in the status line)
computed from objective signals (last verified progress, test trend, message count).

### Candidate C — Provenance / audit panel (per-claim verification status)
**What:** For team/subagent runs, log every claim with the evidence it actually
observed (files read, commands run, outputs) and tag each claim
verified / relayed / unverified (defense against "Audit Without Verification"), plus
a "considerate participation" note for blocked work.
**Why:** Audit Without Verification (accountability layers RELAY, don't CHECK -
filed reports are the primary artifact), Finishing the Task Is Not Enough
(resilience + considerate participation as first-class criteria).
**Slice:** an `/audit` command that reads the team run's worker sessions + tags the
claims by verification status.

### Candidate D — Evolvable harness profiles (self-update with a diff + gate)
**What:** Maintain a per-task-family harness profile (system prompt, tool config,
loop params) and let the agent propose small edits after a run, stored in the
continual layer, shown as a visible DIFF with a user approve/reject gate.
**Why:** Ecdysis, Harness-of-Harness, Hierarchical Self-Improvement, Evo-Harness
(improving the harness, not the model, lifts long-horizon performance); the "harness
is the product" theme. Keep a safety review of the distilled change (Practice Makes
Unsafe).
**Slice:** a `/harness` command that shows the current profile + a `/harness
propose <text>` that stages a proposed edit for review (a visible diff + gate),
never auto-applying.

### Candidate E — Subagent fan-out with progress-aware routing
**What:** For hard tasks, spawn 2-4 short-lived subagents with fresh context on
different approaches; route/terminate them on MEASURED progress (tests/diffs), not
self-reported status.
**Why:** SwarmResearch (one long context converges prematurely; parallel fresh
contexts explore superior alternatives), ClawArena-Team (subagent orchestration is
the production pattern), ProgRouter + TROVE (progress-guided routing on evidence).
**Slice:** a `/swarm <goal>` command that spawns parallel fresh-context candidates
and reports their measured progress (a safe, scoped version of the team mode).

### Candidate F — Capability-scoped tool authority (prompt-injection defense) [DEFERRED]
**What:** Replace "naming a resource grants access" with explicit scoped
permissions (resource x action x duration); treat content read from files/tool
output as DATA, never as instructions; surface the effective permission set +
per-action prompts for out-of-scope actions.
**Why:** Authority Is Not a String / CapScope, When Context Gets Root, Beyond Agent
Harnesses, CAPMAS.
**Status:** DEFERRED - a larger architectural change (modifies the tool executor +
the approval gate); tracked for a future phase.

### Candidate G — Portable versioned skill packs + local security scan [PARTIAL]
**What:** A `skills` subsystem: list/install/enable versioned markdown+code skills
(compatible with openai/anthropics formats) + a local SkillSpector-style scanner
(vuln/malicious-pattern detection) before enablement.
**Why:** The skills ecosystem is the fastest-moving part of the field (multiple
1k-10k stars/week; two official vendor catalogs; SkillSpector security scanning).
**Status:** PARTIAL - nbchat already has a skill system + the `refine`/continual
harness; the *security scan* + *versioned portable packs* are the new slices.

---

## Decision: what to implement in THIS wave

Chosen (safe, additive, high-leverage, testable): **A, B, C, D** (in that order of
leverage). These map directly to the highest-convergence findings and are each a
scoped, testable slice that does NOT modify the v1 REPL or the tui2 base.

Deferred to a future wave (larger / more invasive): **E** (subagent fan-out routing
- touches the team/agent turn logic), **F** (capability-scoped authority -
architectural), **G** (skill security scan + portable packs - new subsystem).

## Implementation + results
(added as each candidate is implemented - see docs/tui3_roadmap.md)
