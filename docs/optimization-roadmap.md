# Optimization Roadmap — Compounding an Autonomous Task-Completion Agent

Status: findings + ordered plan (v1). Frontier research conducted 2026-09-10.
Companion to: `docs/throughput.md`, `docs/c8-saturation-assessment.md`,
`docs/efficiency_review.md`, `docs/task_tracking.md`, `bench/THROUGHPUT_FINDINGS.md`.

This document is a living tracker. Section 9 logs each implemented improvement.

---

## 1. The question, and the mental model

We have a **text+vision model inside a multi-agent harness** that must become a
capable autonomous task-completion agent. Three hard constraints:

- **Total token throughput** (model/quant + inference engine + hardware).
- **Response quality** (model + quant).
- **Harness capabilities** (what the loop/orchestration can actually do).

The user's core insight is the one that matters most: **improvements compound,
so ORDER is a first-class design variable.** Doubling tok/s doubles throughput
*and* the rate at which we can run R&D to find the next improvement; bringing
parallelism online multiplies task-completion speed *and* the amount of
parallelizable R&D the system can do. Early multipliers are worth doing first
because they shorten the time-to-value of everything that comes after.

To reason about order, define a productivity model. For a fixed wall-clock
window, useful work completed is approximately:

```
Work = T x P x E x Q x V
```

- **T** = engine throughput (aggregate tok/s under real batch).
- **P** = effective parallelism (workers running useful, independent work at once).
- **E** = token efficiency (fraction of tokens that are *useful* vs. re-reads,
  retries, verbose reasoning, redundant context).
- **Q** = first-pass quality (fraction of tasks solved without rework).
- **V** = verification factor (does the system catch its own errors before they
  become expensive downstream failures / rework?).

A given improvement raises one or more of these. Two properties decide its
order:

1. **Impact** — how much it moves Work.
2. **Compounding coefficient** — how much it *shrinks the time-to-value of all
   subsequent improvements*. (A throughput or parallelism multiplier does this;
   a one-off prompt fix usually does not.)

**The single most compounding, lowest-risk early investment is instrumentation
that turns "recursive improvement" into a measured loop.** If every improvement
is scored on the same benchmark, we stop guessing at order and start optimizing
against data — which is itself a multiplier on every later decision. (See
Phase 0.)

---

## 2. What is already in place (do not duplicate)

Grounded in the measured local docs (these corrected several earlier claims):

| Capability | State | Evidence |
|---|---|---|
| Engine throughput (measured) | C1 515→C8 265 tok/s per-turn; **C4 433 tok/s is the operating sweet spot**; prefill 2.9k–8.7k tok/s | `docs/throughput.md` |
| KV pool headroom | 1,080 active context slots @128k; C8 uses 38.9% at p50; no spill/eviction observed | `docs/throughput.md` |
| Parallel orchestration (`/team`) | Planner→DAG→replanner→synthesis; pool + reaper; **`team_max_workers` already = 8** | `repo_config.yaml:148`, `docs/c8-saturation-assessment.md` |
| Multi-task speedup | 8 tasks: 200 s serial (25 tok/s eff.) vs 60–90 s parallel | `docs/throughput.md` |
| Context management | Token-budget windowing, L2 importance retrieval, lossless window, summarizer, `keep_recent_exchanges` | `repo_config.yaml` |
| Reasoning-effort control | `default_reasoning_effort: medium`; per-session `/effort`; xhigh vs medium is a large token delta | `repo_config.yaml`, `bench/THROUGHPUT_FINDINGS.md` |
| Task instrumentation (v1) | Per-turn task record in SQLite; `TEAM_METRICS_ENABLED: true` | `docs/task_tracking.md` |
| Error recovery | Tolerant JSON repair ladder, stream-retry + continuation nudge, hang guard | last commit, `core/retry.py` |

So the "bring parallelism online" example the user raised is **largely done**
(workers=8). The next multipliers are elsewhere — and they are the subject of
this roadmap.

---

## 3. Frontier color (what the field is doing right now)

Synthesized from the Anthropic engineering series (Dec 2024 – Sep 2025) and the
llama.cpp prefix/KV-cache design. Key findings, with the number that matters:

**Multi-agent token economics.** Agents use ~4× the tokens of a chat; a
multi-agent system uses ~**15×** the tokens of a chat. That is the cost of
parallelism. The payoff: a multi-agent research system (strong lead agent +
smaller subagents) **outperformed a single top model by 90.2%** on an internal
research eval, and **token usage alone explained 80% of the variance** in
agent performance (tool calls + model choice explained ~15% more). Two
consequences for us:

- *Spend tokens where they buy performance* — parallel breadth-first work is
  where the multiplier is real.
- *The 15× burn is a bug to fight, not a fact to accept* — every point of
  token-efficiency (E) is worth 15× more than it is in a chat.

**Subagents as compression.** The stated reason multi-agent works is that each
subagent "distills insights from a vast corpus" and returns only the key tokens
to the lead. Our `/team` synthesis step is the analog. **Trimming what each
worker returns to the orchestrator is a direct E multiplier and cuts the 15×
burn.** (This is the highest-leverage context move, more than raw compaction.)

**Context rot is real and universal.** As context grows, recall degrades
(needle-in-haystack), across all models. It is attributed to the model's
"attention budget" and the **n² pairwise attention** over a long context. The
practical rule: *treat context as a finite resource with diminishing marginal
returns* and curate it **every turn**. We already do token-budget windowing;
the frontier refinement is to keep per-task context *short and high-signal*
(routing) and push the long, low-signal stuff to retrieval/subagents.

**Start simple; workflows before agents.** The most successful production
systems used composable patterns, not complex frameworks. For well-defined
recurring sub-tasks, a *workflow* (fixed code path + LLM calls) is cheaper and
more predictable than a free-running agent — and it burns fewer tokens. In our
harness this means: **not every subtask needs a full agent**; cheap,
deterministic-shaped subtasks can run as a tight tool-loop with low reasoning
effort.

**Throughput multipliers the engine side offers (llama.cpp):** server-side
**prefix / KV-cache reuse** (shared system prompt + tool schemas + repo overview
+ plan are prefill once, reused by all 8 workers), continuous batching (we have
it), and **speculative decoding / draft-model** for a pure T multiplier. These
are engine-side, so they are listed as coordination items (they live in ninfer,
not this repo).

---

## 4. The ranked, ordered plan (compounding-first)

Ordering principle: **instrument → near-free multipliers (T, P, E) → quality
multipliers (Q, V) → structural multipliers → point the machine at itself.**
Each phase is chosen to raise the compounding coefficient before we spend effort
on things that only raise a single factor.

| # | Improvement | Factor | Impact | Effort | Compounds | Why this order |
|---|---|---|---|---|---|---|
| **0** | **Standard benchmark task-set + one-page scorecard** (wall-clock, tokens/task, rework rate, re-read rate) over the SQLite task store | all | High | Low | **Very high** | Turns recursive improvement into a *measured* loop. De-risks every later choice. We have the data (v1) — we lack the fixed yardstick. |
| **1a** | **Confirm prefix/KV reuse across the 8 workers** (server-side) | T, E | High | Low | High | All workers share system prompt+tools+repo overview+plan. Prefill once, decode for all. Near-free, and it scales with P. *Engine-side — coordinate with ninfer.* |
| **1b** | **Adaptive reasoning effort** (default medium; escalate on stall/failure; de-escalate for trivial tool-shaped subtasks) | E, T | High | Low–Med | High | Directly moves the 41,696-token/turn figure down. Multiplies with T: fewer wasted tokens = more useful tok/s. |
| **1c** | **Parallel / batched tool calls** (issue independent tool calls in one round-trip instead of serial) | E, T | Med–High | Med | High | Cuts loop round-trips and lets the server batch; every saved round-trip is saved across all workers. |
| **2a** | **Subagent distillation** (each `/team` worker returns a tight "findings" block; orchestrator keeps only key tokens) | E, Q | High | Low–Med | High | Directly attacks the 15× multi-agent token burn (frontier #1 finding). Raises Q by keeping the orchestrator's context high-signal. |
| **2b** | **Session-scoped cache for idempotent read tools** (cat/read/grep/ls) — **DONE** | E | Med | Low–Med | Med | Redundant re-reads were **48% of measured tokens** (29,352/41,696). A "cached, unchanged" read-through cache removes that waste without changing semantics. Implemented 2026-09-10 (conservative closed allow-list + mutation flush; see §9). |
| **2c** | **Cheap verifier / test-gate before synthesis** (run the task's own acceptance check; block on failure) | V, Q | High | Med | High | Catches errors *before* they become downstream rework. In a recursive R&D system, early error-catch compounds: it prevents wasted parallel work built on a wrong premise. |
| **3a** | **Model tiering / router** (small model for routing + cheap tools, big model for hard reasoning) | E, T | High | Med–High | High | Raises E and T simultaneously; the frontier's "upgrade model > double token budget" argues the big model should be spent *surgically*. |
| **3b** | **Speculative decoding / draft model** (engine) | T | Med | Med–High | Med | Pure T multiplier, independent of everything else, but more setup than prefix cache → later. *Engine-side.* |
| **3c** | **Persistent knowledge base / RAG for repo + past runs** | Q, E | Med | Med | Med | Cuts re-derivation of repo facts and past decisions; grows more valuable the more the agent runs. |
| **4** | **Recursive self-improvement loop** (point the now-fast, now-measured system at improving the harness, with the scorecard as guardrail) | all | High | High | **Definitional** | Only once 0–3 are in place is "the agent improves itself" a controlled, measured process rather than drift. |

**The critical path is 0 → 1a/1b/1c → 2a/2b/2c.** That sequence maximizes
compounding per unit effort. Everything after is additive on top.

---

## 5. Concrete, immediately actionable items

1. **Build the scorecard (Phase 0).** One SQL over the task store + a fixed 5-task
   benchmark (mix of breadth-first and sequential) gives: wall-clock makespan,
   tokens/task, tool-calls/task, rework rate (tasks redone), re-read rate. This
   is the yardstick for every later change and the substrate for Phase 4.
2. **1b Adaptive effort** is the most self-contained *harness* win (no engine
   dependency): a stall/failure-driven escalation + a "trivial subtask"
   de-escalation. Contained in the loop; testable.
3. **2a Subagent distillation** is a bounded change to the `/team` synthesis
   contract (worker → orchestrator message shape). High frontier-validated E.
4. **2b Read cache** is bounded and directly removes the measured 48% re-read
   waste; must be semantics-safe (invalidate on file mtime change / explicit
   refresh).

---

## 6. What NOT to do early (and why)

- **Raising worker count past 8 or chasing C>8 aggregate tok/s.** Measured data
  shows C8 (334 tok/s) is *below* C4 (433 tok/s) under long contexts — more
  width is **not monotonic** and KV pressure is the limit. Width is already at 8.
- **Planner redesign.** Plan parsing/DAG/caps are tested and correct (per the
  C8 doc). Do not spend compounding-budget here.
- **A "smarter" big-model rewrite of the orchestrator.** The frontier result is
  that *token spend* and *parallelism* dominate; re-architecting the orchestrator
  without measuring first risks Q regression for uncertain gain.
- **Multi-agent for tightly-coupled, sequential, or shared-context subtasks.**
  Frontier evidence: multi-agent shines on breadth-first *independent* work and
  loses on tightly-coupled code/state. Route such subtasks to a single agent or
  a workflow.
- **Speculative decoding before prefix caching.** Larger setup, smaller/narrower
  multiplier, engine-side. Order it after the near-free wins.

---

## 7. Risks

- **15× token burn** can outrun a fixed throughput budget the moment we default
  to parallelism. Mitigation: Phase 1b/2a (effort + distillation) cap the burn.
- **Context rot** degrades Q as per-task context grows; mitigation: keep
  orchestrator context short (2a/3a) and push detail to retrieval.
- **Read-cache correctness** (2b) is the main risk of a "free" E win; must be
  mtime/refresh-gated and test-verified.
- **Adaptive-effort oscillation** (1b): escalation must be hysteresis-gated to
  avoid flapping between low/high effort every turn.

---

## 8. Open questions for the user

1. **Engine access:** can we make (or verify) the llama.cpp/ninfer server-side
   **prefix/KV cache reuse** and **speculative decoding** changes, or is the
   engine frozen for now? (Items 1a, 3b are engine-side.)
2. **Vision:** is the text+vision capability exercised in the target tasks
   (screenshots, PDFs, diagrams)? If yes, vision-token cost is another E lever
   (downsample / crop before the model) I can add a phase for.
3. **Benchmark set:** do you have a canonical task set I should pin as the
   Phase 0 yardstick, or should I draft a 5-task mix and you approve?
4. **Hardware ceiling:** is the single-GPU / current-quant the fixed target, or
   is a larger quant or a second device on the table? (Changes the 3a tiering
   math.)

---

## 9. Progress tracker

| Phase | Item | Status | Commit | Notes |
|---|---|---|---|---|
| 0 | Findings + roadmap doc | done | — | this file |
| 0 | Scorecard + benchmark task-set | pending | — | |
| 1b | Adaptive reasoning effort | pending | — | |
| 1c | Parallel / batched tool calls | pending | — | |
| 2a | Subagent distillation | pending | — | |
| 2b | Idempotent read-tool cache | done | 803e50b | `nbchat/core/tool_executor.py`: closed allow-list classifier (ls/cat/head/tail/wc/grep/rg/diff/file/stat/tree/pwd/whoami/date/df/du/which + git reads; `sed -i` excluded); 64-entry LRU; unclassified `run_command` and all mutating tools flush it, so no stale read survives a mutation. 31 tests in `tests/test_read_cache.py`; suite 455 passing. |
| 2c | Pre-synthesis verifier / test-gate | pending | — | |
| 3a | Model tiering / router | pending | — | |
| 1a/3b | Engine: prefix-cache reuse / spec decode | pending | — | engine-side, awaiting user (Q1) |
| 4 | Recursive self-improvement loop | pending | — | guardrailed by scorecard |
