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
- Major research labs + academic institutions: OpenAI, Anthropic, DeepMind, Meta,
  Microsoft Research, Hugging Face, LangChain, Cognition, OpenHands, Aider/SWE-agent,
  Princeton/Stanford/CMU - direct HTTP fetch of lab blogs, research pages, RSS feeds,
  and sitemaps (res-labs: `/tmp/nbchat_academic_research/labs.md`).
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

## Research-lab findings (res-labs: OpenAI, Anthropic, DeepMind, Meta, MSFT, HF, LangChain, Cognition, OpenHands, Aider/SWE-agent, Princeton/Stanford/CMU)

Method: direct HTTP fetch (requests + BeautifulSoup) of lab blogs, research
pages, RSS feeds, and sitemaps; Cloudflare-blocked sites (OpenAI) accessed via a
text proxy. Full raw HTML/text cached under `/tmp/nbchat_academic_research/`.

**Convergent themes across the labs (2025 -> 2026):**

1. **The harness is the product; the model is a swappable component.** "Agent =
   Model + Harness" (LangChain). OpenAI's zero-human-code product (~1M LOC, ~1,500
   PRs, 0 lines of manually written code), Anthropic's managed agents, MSFT's
   SkillOpt/Orchard, and SWE-agent's pivot to a minimal loop all agree: with
   frontier models fixed, nearly all performance + cost headroom lives in harness
   artifacts - context policy, tools, docs, plans, evals.
2. **Context engineering supersedes prompt engineering.** Give a **map, not a
   manual** (AGENTS.md as a ~100-line TOC over a structured docs/ knowledge base);
   **progressive disclosure** with mechanically verified freshness (linters,
   doc-gardening agents); **context rot is real** (intelligence degrades with
   context length - fresh-context subagents + structural compaction beat prompt
   tricks); **prefix-stable prompts for caching** (static content first, forked
   subagents exploit prompt caching); **the filesystem as the context medium**
   (plans, decision logs, evidence as versioned artifacts in-repo).
3. **Single-writer, multi-intelligence is the working multi-agent pattern.**
   Cognition: parallel writers fail; writes stay single-threaded while other
   agents contribute intelligence (clean-context reviewers, "smart friend"
   escalation, manager/children). Anthropic: Opus lead + parallel Sonnet
   subagents (+90.2%). LangChain deepagents: workers fork context, verifiers stay
   isolated. OpenAI: agent-to-agent review loops drive PRs to completion with
   humans only at judgment points. Unstructured swarms are called a distraction.
4. **Verification is a first-class, always-on loop.** Devin Review (~2 bugs/PR,
   58% severe, clean context beats shared context due to context rot);
   agent-to-agent review until all reviewers are satisfied (OpenAI); value-model
   reranking of traces (Orchard); agent-audited benchmarks (~30% of SWE-bench Pro
   tasks found broken); self-correcting memory with drift/poisoning checks
   (LangChain); "record a video of the failure and of the fix" (OpenAI).
5. **Memory is becoming agent-owned data, not a vendor service.** HF funes
   ("a memory is a dataset, not a service"; traces -> local dataset -> hybrid
   retrieval with exact provenance); LangChain ("your harness, your memory";
   closed-harness lock-in warning); MSFT Memora (decoupled storage/retrieval);
   OpenAI (in-repo knowledge is the system of record).
6. **Containment + blast-radius engineering are now standard harness
   components.** MicroVM isolation per session (Cognition); per-worktree ephemeral
   environments with CDP + observability (OpenAI); OS-style virtualized
   brains/hands (Anthropic); egress control + risk-classified autonomy (measured
   approval fatigue at 93%); Windows sandbox (OpenAI); loss-of-control evals
   (Meta).
7. **Self-improving harnesses (train the harness, not the weights).** SkillOpt
   (skill files edited under validation gates, best across 52 eval cells with no
   weight changes); LangChain (evals as harness training data, hill-climbing);
   OpenAI (doc-gardening + background GC agents).

**res-labs TOP 5 ideas for a coding-agent TUI (ranked):**

1. **Clean-context reviewer subagent + communication bridge** (Cognition ~2
   bugs/PR, 58% severe; clean context beats shared context; the bridge prevents
   loops/scope drift) - a `/review` command + a findings table. *Highest-cited
   pattern in 2026 writing; a safe, high-leverage slice (a future wave).*
2. **AGENTS.md-as-TOC + linted docs/ knowledge base + doc-gardening agent**
   (OpenAI's zero-human-code program) - an `/init-repo-knowledge` command + a
   docs-freshness status line. *nbchat already auto-loads AGENTS.md/CLAUDE.md
   (tui2); the TOC + doc-gardening are the new slices.*
3. **Context modes for subagents: fork for workers, isolated for verifiers**
   (LangChain deepagents; OpenAI Codex loop) - expose a context mode + estimated
   cache-hit saving when spawning subagents.
4. **Trace-based local memory with provenance (agent-owned dataset)** (HF funes,
   exactly this architecture; LangChain lock-in warning; MSFT Memora) -
   `/remember`/`recall` commands + a `mem` status line. *Answers the #1 cited
   pain point: every new session meets the project as a stranger.*
5. **Trainable skill files with validation-gated edits + a smart-friend tool**
   (MSFT SkillOpt; LangChain hill-climbing; Cognition smart friend) - a `/skills`
   browser + a `harness-opt` background job. *The long-term moat.*

**How the lab findings converge with the arXiv + repos findings:** the labs
INDEPENDENTLY confirm every recurring theme in the arXiv cluster (harness-as-
product, context engineering, single-writer multi-intelligence, always-on
verification, agent-owned memory, self-improving harnesses). The flagship
terminal-agent result (T1) + the verifiers-as-process-rewards recipe (Candidate
A) are the labs' "verification is a first-class, always-on loop" theme made
concrete. The res-labs #1 idea (clean-context reviewer) is the
verifier-gated-teams theme (res-arxiv theme 6) made operational; it is deferred
to a future wave (it touches the team/agent turn logic, like Candidate E).
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

All four candidates (A, B, C, D) were implemented in `nbchat/tui3/app.py` (a
subclass of the tui2 `ChatApp`), each as a safe, additive, testable slice that
does NOT modify the v1 REPL or the tui2 base. Each is intercepted in
`_run_command` BEFORE the tui2 dispatch (via the `_TUI3_NATIVE` tuple), so the
version-to-feature-set boundary is explicit. Committed + pushed + remote-verified.

| Candidate | Feature | Commit | Test suite | Worthwhile? |
|-----------|---------|--------|------------|-------------|
| A | `/verify` + live process-score pill | `f708433` | tui3 35 | **YES** - the single highest-leverage reliability feature (the T1 recipe) |
| B | `/health` + rot indicator pill | `fabaabf` | tui3 41 | **YES** - objective long-run health, defends against "agent rot" |
| C | `/audit` provenance panel | `71ac26b` | tui3 46 | **YES** - a concrete, measurable trust signal (verified vs. relayed vs. unverified) |
| D | `/profile` evolvable harness profile + user gate | `3972fe7` | tui3 53 | **YES** - the harness-as-optimization-artifact theme, with a non-invasive user gate |

### Results detail

**A — `/verify` (verifier-driven process score).** `_verify_data` reads the DB
history (most-recent-first) and prefers the latest `run_tests` JSON result (a
0-100 score from the pass-rate), falling back to the latest `run_command`
`exit_code` (ok / FAIL). `/verify` shows the score + pass/fail breakdown + a
clean / not-clean verdict. The `_verify_pill` shows a live status-line pill
(`tests 3/5` when clean, `tests 2F` on failures, `check ok` / `check FAIL` for
run_command). **Worthwhile:** directly implements the flagship T1 recipe (live
verifier-driven process score) + the "objective, not self-reported" reliability
theme. 9 tests.

**B — `/health` (objective long-run rot monitor).** `_health_data` computes
objective signals: message count (context bloat), turn count, the current
verifier score (from A), the test-suite trend (the last two `run_tests`
pass-rates), and the time since the last verified progress (a DB timestamp
query). The `/health` report flags RISK when any signal degrades (context bloat
> 200 messages, verifier score < 50%, test trend regressing, > 30 min since the
last verified progress). The `_health_pill` shows a live status-line pill only
when at risk (e.g. `ROT 32m` or `ROT 10%`). **Worthwhile:** a concrete,
objective defense against "agent rot" + the unreliable-progress-bar problem.
6 tests.

**C — `/audit` (provenance / audit panel).** `_classify_claim` tags every tool
call by VERIFICATION STATUS: `verified` (backed by an objective signal - a clean
test result or a zero exit code), `relayed` (a text summary with no objective
check), or `unverified` (errored / a failed check). `/audit` shows the verified /
relayed / unverified counts, the 5 most recent claims, and a note when every
claim is relayed (no objective check yet) or when there are unverified claims.
**Worthwhile:** a concrete, measurable trust signal - shows HOW MUCH of the
session's claims are actually CHECKED vs. merely RELAYED (the defense against
"Audit Without Verification"). 5 tests.

**D — `/profile` (evolvable harness profile + user gate).** The harness keeps a
structured, VERSIONED per-task profile (prompt template, allowed tools, memory
policy, verification rules) in a JSON file (`NBCHAT_TUI3_PROFILE` env override,
default `~/.nbchat/tui3-profile.json`). `/profile set <key> <value>` STAGES an
edit (never applied directly) + shows the key-level diff. `/profile diff` shows
the staged-vs-applied diff. `/profile apply` applies the staged profile (the USER
GATE). `/profile discard` clears the staged profile. **Worthwhile:** implements
the "the harness is the primary optimization artifact" theme (Evo-Harness,
Ecdysis) with a non-invasive, always-safe user gate - the agent can propose
harness changes, but the USER gates every change. 7 tests.

### Regression status

Full suite after all four: tui3 53, tui2 376, v1 79, team_metrics_main 5, core
team 54, core 295 (all green; one flaky async-thread v1 test passes on re-run).
No changes to `nbchat/tui/app.py` (v1) or the tui2 base. `repo_config.yaml`
clean.

