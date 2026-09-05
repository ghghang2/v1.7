# A Multi-Agent Framework Designed to Saturate C=8 Decode Concurrency

## Design constraints this framework is built around

Everything here follows from what we established about the serving layer:

- `max_concurrency=C` is a **fixed pool of decode lanes**, not a batch size you request per-call.
- **Prefill is serialized** — only decode is cohort-batched. Bursting many large prompts at once queues them on prefill and inflates TTFT without helping throughput.
- Freed lanes backfill **dynamically** ("safe round boundary" admission), so a queue that's always ≥ C deep keeps the pool saturated; a queue that occasionally drains does not.
- Tool execution (bash, browser, file I/O) is **decode-idle time** — an agent waiting on a tool holds no lane, so logical agent count needs to exceed C for lanes to stay full during those gaps.
- `kv-capacity auto` resolves to `C × per-request-context-ceiling` — oversizing either one risks the memory-pressure regression where C=8 is *slower* than C=4.
- Higher C is not always better — it must be swept empirically per workload.

The central design principle: **decouple the logical task graph (which should have many more than C runnable nodes) from the physical decode-lane count (C), via one shared queue and one dispatcher.** No component here maps "an agent" to "a lane" — that mapping is exactly what under-fills the pool.

## Architecture

```
                     ┌───────────────────────────────────────────┐
                     │              Task Graph / Orchestrator      │
                     │  (planner, workers, verifiers, fan-out —    │
                     │   many more runnable nodes than C)          │
                     └───────────────────┬─────────────────────────┘
                                         │ spawns (oversubscribed: ~2.5×C alive)
                     ┌───────────────────▼─────────────────────────┐
                     │              Agent Pool (async)              │
                     │  each agent: build prompt → enqueue → await  │
                     │  → run tool (lane-free) → repeat             │
                     └───────────────────┬─────────────────────────┘
                                         │ LLMRequest(priority, prompt, ctx_ceiling)
                     ┌───────────────────▼─────────────────────────┐
                     │           Shared Priority TaskQueue          │
                     │   (cross-agent — NOT one queue per agent)    │
                     └───────────────────┬─────────────────────────┘
                                         │ pop() — prefill-aware ordering
                     ┌───────────────────▼─────────────────────────┐
                     │   Dispatcher  +  ContextBudgetGuard          │
                     │   loop: while free_lane and queue not empty: │
                     │     admit next request                       │
                     │   if queue drains: ask FanOutFiller for work │
                     └───────────────────┬─────────────────────────┘
                                         │ acquire lane (semaphore, size C)
                     ┌───────────────────▼─────────────────────────┐
                     │        DecodeLanePool  →  ninfer-serve       │
                     │        (--max-concurrency C)                 │
                     └───────────────────┬─────────────────────────┘
                                         │ completion → release lane
                     ┌───────────────────▼─────────────────────────┐
                     │   Telemetry (per-request timings, MTP        │
                     │   acceptance, lane utilization) → eval       │
                     │   harness / ConcurrencyAutotuner              │
                     └───────────────────────────────────────────────┘
```

## Component pseudocode

### 1. DecodeLanePool — models `max_concurrency` as a resource, not a batch call

```python
class DecodeLanePool:
    """One instance per ninfer-serve process. Owns exactly C physical lanes."""

    def __init__(self, C, endpoint):
        self.C = C
        self.lane_semaphore = Semaphore(C)
        self.endpoint = endpoint
        self.inflight = {}  # request_id -> (submit_time, est_prompt_tokens)

    async def submit(self, request):
        await self.lane_semaphore.acquire()          # blocks only if all C lanes busy
        self.inflight[request.id] = (now(), request.est_prompt_tokens)
        try:
            response = await self.endpoint.chat_completions(
                model=request.model_id,
                messages=request.messages,
                max_tokens=request.max_tokens,
                seed=request.seed,
            )
            record_telemetry(request, response)       # feeds ConcurrencyAutotuner + eval harness
            return response
        finally:
            del self.inflight[request.id]
            self.lane_semaphore.release()             # lane is freed the instant THIS request's
                                                        # decode finishes, not when a cohort finishes —
                                                        # matches the engine's dynamic backfill behavior

    def utilization(self):
        return len(self.inflight) / self.C
```

### 2. Shared TaskQueue — one queue for the entire fleet, not per agent

```python
class LLMRequest:
    id: UUID
    agent_id: str
    priority: int              # lower = more urgent (critical path > filler)
    messages: list
    est_prompt_tokens: int      # from a cheap local estimate or /count_tokens
    max_tokens: int
    ctx_ceiling: int            # PER-REQUEST context reservation, sized to need, not model max
    future: Future              # resolved by the Dispatcher on completion

class TaskQueue:
    def __init__(self):
        self._heap = []  # (priority, prefill_cost_bucket, insertion_order, request)

    def push(self, request):
        bucket = prefill_cost_bucket(request.est_prompt_tokens)  # see Dispatcher below
        heappush(self._heap, (request.priority, bucket, next(_seq), request))

    def pop(self):
        return heappop(self._heap)[-1]

    def peek_depth(self):
        return len(self._heap)
```

### 3. Dispatcher — the saturation loop, with prefill-aware ordering

Since prefill is serialized and decode is not, the dispatcher deliberately avoids letting several huge-prompt requests monopolize the prefill stage back-to-back — that would stall every lane's *admission* even though decode itself would batch fine once running.

```python
class Dispatcher:
    def __init__(self, lane_pool: DecodeLanePool, queue: TaskQueue,
                 context_guard: "ContextBudgetGuard", filler: "FanOutFiller"):
        self.lane_pool = lane_pool
        self.queue = queue
        self.context_guard = context_guard
        self.filler = filler

    async def run_forever(self):
        while True:
            if self.lane_pool.utilization() < 1.0 and self.queue.peek_depth() > 0:
                request = self.queue.pop()

                if not self.context_guard.admit(request):
                    # would blow the reserved KV pool (C × ctx_ceiling) — requeue smaller
                    request.ctx_ceiling = self.context_guard.suggest_ceiling(request)
                    self.queue.push(request)
                    continue

                spawn_task(self._run_and_resolve(request))   # non-blocking: don't await here,
                                                               # or you serialize admissions yourself

            elif self.queue.peek_depth() == 0 and self.lane_pool.utilization() < TARGET_UTIL:
                # organic demand dried up — don't let a lane sit idle, manufacture useful work
                for filler_request in self.filler.propose(n=self.lane_pool.C):
                    self.queue.push(filler_request)

            await sleep_or_wake_on_event(POLL_INTERVAL)

    async def _run_and_resolve(self, request):
        response = await self.lane_pool.submit(request)
        self.context_guard.release(request)
        request.future.set_result(response)


def prefill_cost_bucket(est_prompt_tokens):
    """Coarse bucketing so the priority queue naturally interleaves short and long
    prompts instead of admitting several huge prefills back-to-back."""
    if est_prompt_tokens < 2_000:   return 0   # cheap, admit eagerly
    if est_prompt_tokens < 20_000:  return 1
    return 2                                    # expensive — admit but don't let 2+ queue together
```

### 4. ContextBudgetGuard — keeps `C × ctx_ceiling` inside the real KV budget

```python
class ContextBudgetGuard:
    def __init__(self, total_kv_capacity_tokens):
        self.total = total_kv_capacity_tokens
        self.reserved = 0

    def admit(self, request):
        if self.reserved + request.ctx_ceiling > self.total:
            return False
        self.reserved += request.ctx_ceiling
        return True

    def release(self, request):
        self.reserved -= request.ctx_ceiling

    def suggest_ceiling(self, request):
        # shrink to fit rather than starving the queue entirely; agents should be
        # designed to tolerate a smaller ceiling on retry (e.g. summarized history)
        return max(request.min_viable_ctx, self.total - self.reserved)
```

### 5. PromptBuilder — one byte-identical global prefix, for cache reuse across *any* lane

Prefix reuse benefits any request landing in any lane, provided the prefix is identical — so the stability discipline is fleet-wide, not per-agent.

```python
GLOBAL_STABLE_PREFIX = render_once(system_prompt, tool_schemas)  # frozen for the process lifetime

class PromptBuilder:
    @staticmethod
    def build(agent_state):
        # Variable content is strictly appended after the frozen prefix — never
        # spliced into the middle, and any image/video in history must be re-used
        # byte-identical (same encoding/grid) or the cache resets from that point on.
        return GLOBAL_STABLE_PREFIX + agent_state.variable_suffix()
```

### 6. Agent — deliberately oversubscribed, never assumes it owns a lane

```python
class Agent:
    def __init__(self, node, queue: TaskQueue, priority):
        self.node = node          # a runnable unit from the task graph
        self.queue = queue
        self.priority = priority

    async def run(self):
        while not self.node.done:
            prompt = PromptBuilder.build(self.node.state)
            request = LLMRequest(
                id=uuid4(), agent_id=self.node.id, priority=self.priority,
                messages=prompt, est_prompt_tokens=estimate_tokens(prompt),
                max_tokens=self.node.next_output_budget(),
                ctx_ceiling=self.node.min_viable_ctx,
                future=Future(),
            )
            self.queue.push(request)
            response = await request.future        # <-- this await is the whole point:
                                                     #     the agent yields control here and
                                                     #     holds NO lane while queued or blocked

            if response.tool_calls:
                # decode-idle window: no lane held during I/O, which is exactly why
                # oversubscription (more live agents than C) keeps other lanes full
                results = await execute_tools(response.tool_calls)
                self.node.state.append_tool_results(results)
            else:
                self.node.complete(response)
```

### 7. Orchestrator — oversubscribes the pool on purpose

```python
OVERSUBSCRIPTION_RATIO = 2.5   # empirically tuned: enough live agents that tool-blocked
                                # ones don't starve the queue, not so many that scheduling
                                # overhead or context-budget pressure dominates

class Orchestrator:
    def __init__(self, task_graph, queue, lane_pool):
        self.task_graph = task_graph
        self.queue = queue
        self.lane_pool = lane_pool

    def spawn_wave(self):
        target_live = ceil(OVERSUBSCRIPTION_RATIO * self.lane_pool.C)
        ready_nodes = self.task_graph.ready_nodes()
        for node in ready_nodes[:target_live]:
            priority = CRITICAL_PATH if node.is_on_critical_path() else BEST_EFFORT
            spawn_task(Agent(node, self.queue, priority).run())

    def on_node_complete(self, node):
        # topological backfill: as soon as a node finishes, spawn its newly-ready
        # dependents so live-agent count stays near target_live, not just at t=0
        for newly_ready in self.task_graph.unlock_dependents(node):
            spawn_task(Agent(newly_ready, self.queue, priority=BEST_EFFORT).run())
```

### 8. FanOutFiller — fills idle lanes with useful work instead of idling them

When the organic task graph doesn't produce enough concurrent demand (e.g. near the end of a run, or a bottleneck stage with only one ready node), manufacture extra sampling on tasks where more samples improve reliability — this spends otherwise-wasted decode lanes on quality rather than nothing.

```python
class FanOutFiller:
    def __init__(self, task_graph):
        self.task_graph = task_graph

    def propose(self, n):
        candidates = self.task_graph.nodes_eligible_for_resampling()
        # eligible: verifier-critical steps, low-confidence prior outputs,
        # anything where self-consistency / best-of-N genuinely helps correctness
        proposals = []
        for node in candidates[:n]:
            proposals.append(LLMRequest(
                id=uuid4(), agent_id=f"{node.id}-filler-{uuid4()}",
                priority=BEST_EFFORT,       # never outranks real critical-path work
                messages=PromptBuilder.build(node.state),
                est_prompt_tokens=estimate_tokens(node.state),
                max_tokens=node.output_budget,
                ctx_ceiling=node.min_viable_ctx,
                future=Future(),
            ))
        return proposals

    def on_filler_result(self, node, sample):
        node.accumulate_sample(sample)   # majority vote / verifier-selects-best on aggregation
```

### 9. ConcurrencyAutotuner — C is a variable to search, not a constant to assume

```python
class ConcurrencyAutotuner:
    CANDIDATE_C = [1, 2, 4, 8]

    def sweep(self, representative_task_batch):
        results = {}
        for C in self.CANDIDATE_C:
            server = start_ninfer_serve(max_concurrency=C,
                                         kv_capacity="auto",
                                         kv_dtype="int8")   # or fp8 — sweep this too
            makespan, agg_tok_s, oom_events = run_batch(server, representative_task_batch)
            results[C] = dict(makespan=makespan, tok_s=agg_tok_s, memory_pressure=oom_events)
            stop_server(server)

        # do NOT just take argmax(tok_s) — the C=8-slower-than-C=4 regression is real;
        # optimize for corpus makespan under zero memory-pressure events
        viable = {c: r for c, r in results.items() if r["memory_pressure"] == 0}
        return min(viable, key=lambda c: viable[c]["makespan"])
```

## End-to-end wiring

```python
def main():
    C = ConcurrencyAutotuner().sweep(representative_task_batch=load_recent_traces())

    server = start_ninfer_serve(
        max_concurrency=C,
        kv_capacity="auto",
        kv_dtype="int8",                 # re-check against fp8 per the earlier tradeoff
        spec="mtp", draft_tokens=3, lm_head_draft=True,
        prefix_reuse=True,               # do not pass --no-prefix-reuse
    )

    lane_pool = DecodeLanePool(C, endpoint=server)
    queue = TaskQueue()
    context_guard = ContextBudgetGuard(total_kv_capacity_tokens=C * PER_REQUEST_CTX_CEILING)
    task_graph = build_task_graph(job_spec)
    filler = FanOutFiller(task_graph)
    dispatcher = Dispatcher(lane_pool, queue, context_guard, filler)
    orchestrator = Orchestrator(task_graph, queue, lane_pool)

    spawn_task(dispatcher.run_forever())
    orchestrator.spawn_wave()
    task_graph.on_any_complete(orchestrator.on_node_complete)

    await task_graph.until_all_done()
```

## Why each decision maps back to what we learned

| Decision | Reason |
|---|---|
| One shared `TaskQueue`, not one queue per agent | A queue-per-agent-role re-creates the naive 1:1 lane mapping that under-fills the pool during bursty, asynchronous agent turns |
| `OVERSUBSCRIPTION_RATIO ≈ 2.5×C` live agents | Tool calls are decode-idle time; more live agents than lanes means someone is always ready when a lane frees |
| Prefill-aware bucketing in the queue | Prefill is serialized — admitting several huge prompts back-to-back stalls admission for everyone even though decode batches fine once running |
| `ContextBudgetGuard` on `C × ctx_ceiling` | `kv-capacity auto` scales with both C and per-request ceiling; this is the documented mechanism behind the C=8-slower-than-C=4 regression |
| Global, byte-identical `GLOBAL_STABLE_PREFIX` | Prefix reuse benefits whichever lane a request lands in, but only if the prefix is identical fleet-wide, not just per-agent |
| `FanOutFiller` instead of idling free lanes | Organic demand won't always hit C; manufactured best-of-N/self-consistency sampling turns idle capacity into reliability gains instead of waste |
| `ConcurrencyAutotuner` searches C, doesn't assume 8 | The published numbers already show non-monotonic scaling on one model profile; this must be re-measured per workload, not hardcoded |
| Priority field (`CRITICAL_PATH` vs `BEST_EFFORT`) | Filler/exploratory work must never starve real work when lanes are scarce |

## What this framework does not solve

- It doesn't get you past one GPU's aggregate ceiling — `C` lanes on one 5090 is still one 5090's total memory bandwidth and capacity. Beyond that, this is where the earlier "add GPUs, not cleverness" answer applies: run one such stack per GPU and load-balance task-graph shards across them.
- It assumes prompts are cheap to token-estimate locally; if that's not reliable, occasionally call `/v1/messages/count_tokens` for the bucketing decision rather than trusting a rough local heuristic.
- It doesn't account for heterogeneous task graphs where the critical path itself has fewer than C ready nodes for extended stretches — in that regime, filler-based fan-out is doing most of the work of keeping lanes warm, and it's worth checking (via the eval harness) that the extra sampling is actually earning its keep on quality, not just spending GPU time to look busy.
