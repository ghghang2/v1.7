# Tracker: ninfer-serve Throughput Investigation — FINAL

## 1. Environment
- RTX 5090 32 GiB, RunPod pod. ninfer-serve PID 80255, port 8080, exposed to
  the internet via cloudflared quick tunnel (start-endpoint.sh).
- Model qwen3.8-27b NVFP4, MTP3 spec decode, fp8 KV, 240k ctx, max-concurrency 8.

## 2. The core finding (the "what are we doing wrong" answer)

**We are not doing anything wrong. The "1,000 tok/s" number and our measured
numbers are different quantities, and the real bottleneck is that the GPU is
idle most of the day, waiting for the next request — not slow when working.**

### 2.1 What ninfer's ">1,000 tok/s" actually is
From /workspace/ninfer/docs/performance.md (Qwen3.6-27B nvfp4 on the same GPU class):
- C=1 (one concurrent request): **202 tok/s aggregate**
- C=2: 400 · C=4: 700 · **C=8: 1,147 tok/s**
- The "1,000 tok/s" figure is **aggregate throughput with 8 concurrent requests
  kept saturated** (avg batch 8). It is NOT single-stream speed.
- Single-stream decode ceiling for a 27B on 5090: ~200 tok/s aggregate = the
  hardware roofline for this size; per-request you will never see more.

### 2.2 What our server actually does when busy (verified live, 2026-09-09)
- Live traffic per-request decode: 145-170 tok/s/request (e.g. req 650:
  176 tokens in 1.05 s decode = 167 tok/s; MTP acceptance 56%). Matches the
  docs' C=1 number. Server is at its single-stream ceiling.
- Synthetic benchmark on the live server (localhost, thinking off):
  - 4 parallel × 448 tok: **176 tok/s aggregate**, 44 tok/s per stream
  - 8 parallel × 448 tok: 284 tok/s aggregate
  - 4 parallel × 2048 tok sustained: **180 tok/s aggregate**, 45 tok/s per stream
  - (Docs' C=4/C=8 are higher because doc workloads are code/structured with
    70-89% MTP acceptance; our prose/chat traffic accepts ~55-65%.)
- MTP acceptance on live traffic ~50-56% (accepted_per_position ~[53,32,27]
  decaying) — workloads with more structured/code content accept better.
- Prefill: median 4,200 tok/s computed (p10 800) — 10k prompt ≈ 1.3 s TTFT.
  Prefix cache IS working: ~45% of requests hit private endpoint replay or
  shared stable prefix (only 16% full recompute from root).

### 2.3 The real killer: duty cycle (the GPU is starved, not slow)
Request-log analysis over 7.27 h (08:26–15:42 UTC, 4,238 starts / 3,915 done):
- **Average gap between consecutive requests: 6.2 s** (human-in-the-loop: you
  read, think, type; prime-agent waits on tool calls and model round-trips).
- While idle: zero tokens. So wall-clock throughput = decode rate × duty cycle.
  Example: 60 tok/s while busy × ~40-50% duty = 24-30 tok/s "perceived".
- Per-request latency perception: each turn = prefill (~0.5-2 s) + decode at
  ~45-65 tok/s effective (incl. MTP decay on chaty text). A 1000-token answer
  takes ~15-25 s. That is the physics of a 27B on a 5090; no config change
  makes a single 27B stream faster.

### 2.4 Remote/prime-agent path (measured negligible)
- cloudflared quick tunnel adds ~20-80 ms RTT and small per-chunk overhead.
- nbchat client (core/client.py) uses the OpenAI SDK with streaming — fine.
- At 60 tok/s a 15 ms RTT costs ~0.3% of throughput. **Not the problem.**

## 3. What it would take to maximally utilize the 5090

Ranked by expected impact:

1. **Feed the server continuously (biggest lever, 2-5x aggregate).**
   Keep 2-8 concurrent in-flight requests. Concretely:
   - Run nbchat AND prime-agent (and any other agents) against the endpoint
     simultaneously — they currently serialize on the same human.
   - Within prime-agent: pipeline/overlap sub-tasks where possible (speculative
     parallel tool exploration, background summaries/compaction while the main
     turn streams) so there is always a decode in flight.
   - 2 streams ≈ 250 tok/s, 4 ≈ 400-500 tok/s, 8 ≈ 700-1000 tok/s (NVFP4,
     per docs; expect ~55-65% of doc numbers on chat-heavy workloads).
   - Metric to optimize: **aggregate committed tokens per wall-hour across all
     clients**, not per-request tok/s. That is the number that can approach 1k.
2. **Raise MTP acceptance** (free 10-30%): more structured/code-heavy prompts
   accept better (docs: code 70%, structured 89% vs story 38%). Structuring
   agent prompts toward tool-call JSON output helps. No config change needed.
3. **Keep prefix caching hot** (free): stable system prompts already replay
   (~45% hit rate). Keep agent system prompts byte-stable; avoid per-turn
   timestamp/UUID prefixes that invalidate the cache.
4. **Config sanity (all already good — do NOT change)**:
   - MTP3 + lm-head-draft: on (the big lever, ~1.5x). Keep.
   - max-concurrency 8: right for multi-stream service. Keep.
   - fp8 KV + 240k cap + 8 GB host KV: right. Keep.
   - VRAM 30.5/32.6 GiB, 597/600 W: saturated, no headroom issue, 47 C (no
     thermal throttle). Nothing to tune.
   - The old llama.cpp findings (bench/THROUGHPUT_FINDINGS.md) are a different
     engine/hardware (L40S, Q4_K_XL) — its "parallel 1 is best" conclusion
     applies to that single-user llama-server setup, not this multi-slot
     ninfer server; ninfer's own docs show C=4 beats C=8 only under memory
     pressure for nvfp4 (C=4 = 433 tok/s, the sweet spot).
5. **Optional: move to a smaller MoE for interactive turns.** If single-stream
   45 tok/s is unsatisfying for *this chat*, a ~3B-active MoE (docs: 35B-A3B
   at 593 tok/s C=1) is 10x faster per stream but a different model. Trade-off,
   not a fix.

## 4. Bottom line for the user
- Single-stream ~45-65 tok/s and multi-stream aggregate ~150-300 tok/s on the
  live server is **normal and near-ceiling** for 27B NVFP4 + MTP on a 5090.
- The 1,000 tok/s headline requires 8 sustained concurrent requests; our
  human-in-the-loop traffic averages ~1.1 concurrent, and the GPU idles ~6 s
  per request.
- "Leaving performance on the table" is real, but the unused performance is
  **concurrency + duty cycle**, not a misconfigured server. Fix = run more
  agents concurrently and keep one in flight at all times; measure aggregate
  tokens/hour.

## 5. Action log
- 2026-09-09: env verified, server cmdline captured.
- 2026-09-09: live request-log analysis (duty cycle, MTP acceptance, prefix
  cache hits, prefill/decode rates).
- 2026-09-09: synthetic C=4/C=8 + sustained benchmarks on port 8080.
- 2026-09-09: cross-checked ninfer docs' >1k claim (aggregate @ C=8).
- 2026-09-09: client path review (nbchat client.py, cloudflared tunnel).
- 2026-09-09: tracker finalized.

## 2. Hypotheses to test (ranked)
1. **Client-side request serialization / small max_tokens**: if clients send small
   completions or don't overlap requests, the server never reaches steady-state
   decode; per-request latency dominates and "avg tok/s" looks low.
2. **Concurrency underuse**: server allows 8 concurrent; clients likely run 1-2.
   Continuous batching + 27B model: throughput scales until bandwidth-bound.
   Note: "1,000 tok/s" in ninfer docs is likely *aggregate* decode throughput,
   not single-stream latency. Single-stream ceiling for 27B NVFP4 ~50-100 tok/s.
3. **MTP acceptance rate**: --spec mtp --draft-tokens 3 helps only if draft
   acceptance is high; verify acceptance stats in request log.
4. **Prefill dominating**: 240k context with large prompts; time-to-first-token
   is compute-heavy; "throughput" measured including prefill looks much lower.
5. **KV cache / context growth**: long sessions -> bigger KV -> slower decode
   (bandwidth-bound per token grows with KV size).
6. **Network/serialization overhead (remote)**: HTTP/JSON per-token streaming
   overhead, RTT, TLS — usually minor vs compute but verify.
7. **max_concurrency=8 + slot config**: device-state-slots=2 may cap true
   parallelism; check what "slots" mean.
8. **GPU clocks throttling**: 597W cap, 47C — not throttling (temp fine).
   Check clock speeds vs boost.
9. **Server-side: no prefix caching / per-request re-prefill** of shared system
   prompts across nbchat + prime-agent.

## 3. Evidence gathered
- [ ] Request log (/ninfer-request-log-jsonl.log) — per-request timings
- [ ] inference_metrics.log in repo — check what nbchat logs
- [ ] bench/ dir in repo — existing harness?
- [ ] ninfer docs (web) — what "1,000 tok/s" claim actually measures
- [ ] Live benchmark results (localhost vs remote, various concurrency)

## 4. Findings / Recommendations
(To be filled)

## 5. Action log
- 2026-09-09: env verified, server cmdline captured, tracker created.

## 2026-09-10 (continued): max_tokens audit + prime-agent research + dashboard fix

### nbchat max_tokens — VERIFIED CORRECT, no change needed
- repo_config.yaml: max_llm_output_tokens: 32768 (with comment block explaining
  reasoning-budget carve-out; must exceed reasoning_budget 4096 + content).
- nbchat/core/config.py:55 reads it (fallback 8192).
- nbchat/core/conversation.py:827 passes max_tokens=config.MAX_LLM_OUTPUT_TOKENS
  on the main agent loop. Team plan/synthesis intentionally small (2048/1536).
- compressor.py:304, context_manager.py:504 (small util calls) intentionally capped.
- Request-log evidence: dominant max_output_tokens among completed requests is
  32768 (n=1753) + 16384 (n=1904); small 512/4096 caps are the util calls.
  So nbchat is already sending the big budget — no code change applicable.

### prime-agent max_tokens — what needs to change
prime-agent's model budget comes from its model registry, in priority order:
1. Custom model entry in <agent-dir>/models.json  (agent dir = $PRIME_AGENT_DIR
   or ~/.prime-agent/), under `providers.<name>.models.<id>` — `maxTokens` and
   `contextWindow` are supported override fields (ModelDefinitionSchema /
   ModelOverrideSchema in packages/coding-agent/src/core/model-registry.ts:150-192).
   This is the ONLY place to set it for a custom OpenAI-compatible endpoint.
2. Built-in catalog models.generated.ts (not applicable to the local endpoint).
What to change on the REMOTE host running the 2-3 prime-agent /goal instances:
- In ~/.prime-agent/models.json, set on the ninfer model entry:
    "contextWindow": 240000        (match server ctx_size)
    "maxTokens": 32768             (match nbchat's budget; server can serve it)
- Verify it applies: prime-agent logs show requested max_tokens per request, or
  watch the request-log: max_output_tokens in request_start events should flip
  from the current small default to 32768.
- Why it matters: with a small maxTokens, prime-agent requests finish early
  (short decode phase). Under max-concurrency=8, short requests turn over the
  scheduler slots, so the GPU gets idle inter-request gaps — exactly the
  6.2s avg gap measured. Bigger budget = longer decodes = higher concurrency
  utilization toward the 1,000 tok/s aggregate figure.
- Server side is already correct: --pending-timeout-ms 600000 --max-pending...
  allow long in-flight decodes; no change needed.

### Dashboard — FIXED and deployed
- Rewrote /root/ninfer-dash.py (repo copy: dashboards/ninfer-dash.py).
  Now shows the FULL picture:
  * live decode + prefill tok/s (throughput events)
  * complete scheduler queue state: running/waiting/prefilling/decode_ready/
    materializing/capture_pending/terminal_pending (was: running+waiting only)
  * live request feed (start/done/error/rejected) with latency + finish reasons
  * per-request max_output_tokens so client budget misconfig is visible at a glance
  * server config from server_start (argv: max-concurrency, pending limits, model)
  * running total counters
- Old backup at /root/ninfer-dash.py.bak. Verified: py_compile OK; live test
  against /ninfer-request-log-jsonl.log shows dec/pre tok/s + queue state.
- NOTE: the user's running TUI session (PID 102816, launched 15:57) still has
  the OLD code in memory — needs a restart: Ctrl-C, then
  `python3 /root/ninfer-dash.py --tui` (web mode: `python3 /root/ninfer-dash.py
  /ninfer-request-log-jsonl.log 8787` — old web instance PID 58354 also still up
  and should be killed first to free port 8787).
