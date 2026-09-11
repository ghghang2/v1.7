# Multi-Agent Harness Research: Findings & Directions for nbchat.tui3

Research conducted online (2025) via the GitHub API + synthesis of the frontier
multi-agent landscape. The goal: find SUPER-USEFUL features in the most capable
open-source multi-agent harnesses that are NOT yet in ``nbchat.tui2`` and are
PRACTICAL to port (safe, scoped, testable) into a single-process terminal
coding-agent TUI.

## Landscape (2025 open-source multi-agent harnesses, by GitHub stars)

| Harness | Stars | What it is |
|---------|-------|------------|
| bytedance/deer-flow | ~82k | Long-horizon "SuperAgent" harness: researches, codes, creates; sandboxes, memory, tools, skills, subagents, IM gateway (Telegram/Slack), MCP server |
| FoundationAgents/MetaGPT | ~70k | "AI software company" multi-agent framework (role-based: PM, engineer, QA) |
| microsoft/autogen | ~61k | Programming framework for agentic AI (multi-agent conversations) |
| crewAIInc/crewAI | ~58k | Role-based crews + event-driven flows; real-time tracing/observability; control plane |
| agno-agi/agno | ~42k | Agent platform: build, run, manage agents |
| langchain-ai/langgraph | ~41k | Graph-based durable execution; human-in-the-loop interrupts; "deep agents" (plan + subagents + memory); checkpointing/time-travel |
| OpenBMB/ChatDev | ~34k | Multi-agent software dev collaboration |
| openai/openai-agents-python | ~29k | Lightweight multi-agent workflows: agents, handoffs, guardrails, sessions; text/sandbox/realtime/voice agents |
| openai/swarm | ~22k | Educational multi-agent orchestration (handoffs + routines) |
| google/adk-python | ~21k | Code-first agent toolkit: workflow runtime (graph), Task API (structured A2A delegation), rich tool ecosystem, tool-confirmation HITL, deploy-anywhere |
| camel-ai/camel | ~18k | Multi-agent framework; "scaling law of agents" |
| microsoft/agent-framework | ~13k | Framework for building/orchestrating/deploying agents |
| ag2ai/ag2 | ~5k | AG2 (formerly AutoGen) "AgentOS" |
| SWE-agent/SWE-agent | ~20k | Autonomous SWE (coding) agent (SWE-bench) |

## The frontier techniques (what the capable harnesses actually do)

1. **Graph-based orchestration** (LangGraph, Google ADK Workflow Runtime,
   CrewAI Flows): agents/tasks form a graph with explicit state transitions,
   enabling durable execution, retries, and parallel branches.
2. **Human-in-the-loop interrupts** (LangGraph, Google ADK Tool Confirmation,
   CrewAI): the agent PAUSES at defined points (e.g. before a risky tool) and
   waits for the human to approve/edit/reject, then resumes.
3. **Tracing & observability** (CrewAI, LangGraph, OpenAI): every agent step
   (LLM call, tool call, token usage, latency, errors) is traced and can be
   inspected/filtered in real time.
4. **Deep agents / planning loops** (LangGraph Deep Agents): an agent that
   PLANS (decompose into subtasks), EXECUTES (spawns subagents), and
   REFLECTS (self-critique) — a plan-act-reflect loop.
5. **Structured, durable state + checkpointing** (LangGraph, CrewAI):
   durable execution with checkpoints -> replay/time-travel, resume after
   crash, branch from a checkpoint.
6. **Guardrails** (OpenAI Agents): input/output validation + safety rails
   around each step.
7. **Multi-model routing + fallback** (OpenAI, Google ADK, LiteLLM): route
   different subtasks to different models; fall back on context-window /
   rate-limit errors.
8. **Sandboxed execution** (Deer-Flow, OpenAI): code runs in an isolated
   sandbox (container/VM) for safety.
9. **Message gateway / IM channels** (Deer-Flow): reach the agent over
   Telegram/Slack/web (not just the terminal).

## What nbchat.tui2 ALREADY has (do NOT duplicate)

- Subagent teams (spawn/coordinate workers) — covers #1/#4 partially.
- An agentic tool loop + tool executor + tool auto-discovery.
- A **tool-approval gate** (risky tools prompt: approve/deny/always) — covers #2 partially.
- Session history + `/fork`, `/checkpoint`, `/undo`, `/rewind`, `/diff`, `/export` — covers #5 partially.
- `/plan` (plan mode) — covers #4 partially.
- `/stats` (v1), `/model` (v1) — covers #6/#7 minimally.
- A JSON control socket + `--attach`/`--frame` (background-agent workflow) — covers #9 partially.

## TOP features to port into tui3 (ranked by value + practicality)

These are the gaps that are (a) super useful, (b) NOT already in tui2, and
(c) practical to port as a safe, scoped, testable slice.

### Phase 1 — `/trace`: live task-trace / observability view  [HIGHEST VALUE]
A structured, scrollable view of the recent agent activity for the current
session: each tool call (name + key args + result length + ok/error), each LLM
turn, token usage and latency per step, and errors. Reads the EXISTING
conversation history + per-turn stats (no new data plumbing). Inspired by
CrewAI Tracing & Observability + LangGraph durable execution. **Why**:
observability is the #1 thing users ask for when a multi-agent run goes
wrong; it turns an opaque "busy" state into an inspectable trace.

### Phase 2 — approval diff-preview (HITL upgrade)
Upgrade the existing tool-approval gate: for file-mutating tools, show a
PREVIEW (the tool + a short summary of the intended change) and keep the
approve/deny/always flow. Inspired by Google ADK Tool Confirmation +
LangGraph human-in-the-loop. **Why**: the gate already exists; a clearer
preview makes the safety net far more usable (right now it shows a 160-char
arg blob).

### Phase 3 — `/budget`: cost / token tracking + budgets
A cost/token view: totals per session (turns, tool calls, tokens in/out if
available, elapsed time) and an optional per-session token BUDGET that warns
when exceeded. Reads the existing per-turn stats. Inspired by the harnesses'
cost tracking + LiteLLM budgets. **Why**: token/time budgeting is a core
operational need for long runs.

### Phase 4 — deep-agent plan loop (strengthen `/plan`)
Strengthen `/plan` into a plan-act-reflect loop: the agent produces a numbered
plan, the user approves it, then the agent executes step-by-step, checking off
and reflecting. Inspired by LangGraph Deep Agents. **Why**: turns a one-shot
plan into an inspectable, resumable execution.

## Implementation notes
- Every feature is ADDITIVE and lives in ``nbchat/tui3`` (the tui3 ``ChatApp``
  subclass), so the tui2 base is untouched and the version-to-feature-set
  boundary stays explicit.
- Each phase = one feature, tested (unit + a pty E2E where feasible),
  committed, and pushed, with the suite kept green.
