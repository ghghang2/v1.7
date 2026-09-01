# llama.cpp Performance Recommendations — Qwen3.8-27B

Source: r/LocalLLaMA thread "After pushing 1M+ tokens through Qwen 3.8 27B, here is
my optimal llama.cpp config for 16GB VRAM (73k Context, Agentic Coding)"
(https://www.reddit.com/r/LocalLLaMA/comments/1vqrt86/), cross-referenced against our
current `run.py` config and `llama_server.log`.

Status: **NOT YET IMPLEMENTED** — captured for a future tuning pass.

## Our current baseline

| Item | Ours | OP (reference) |
|------|------|----------------|
| GPU | RTX 5090 | RTX 5060 Ti 16GB |
| Model | Qwen3.8-27B-UD-Q4_K_XL | Qwen3.8-27B-UD-Q3_K_XL |
| KV cache | q8_0 | q4_1 |
| ctx_size | 131072 | 73728 |
| Spec decode | `draft-mtp`, n-max=1 | `ngram-mod,draft-mtp`, n-max=2 |
| Sampling | temp 0.65, top_p 0.95, top_k 20, min_p 0.05 | temp 0.65, top_p 0.95, top_k 15, min_p 0.05 |
| context-shift | enabled but **disabled at runtime** (see log) | enabled (`context-shift=1`) |
| n_parallel | 2 | 1 |
| batch / ubatch | 4096 / 4096 | 1024 / 512 |
| Observed decode | ~104 t/s | ~46 t/s |

We are already ahead on the fundamentals (Q4 vs Q3, q8_0 vs q4_1 KV, 5090 vs 5060 Ti,
~104 vs ~46 t/s). Most of the OP's config exists to "fit in 16GB", not to "go faster".
The items below are the genuine gains left on the table for our hardware.

## Recommendations (ordered by impact / confidence)

### 1. ngram-mod + MTP hybrid speculative decode — HIGH impact, HIGH confidence

The top comment (u/pmttyji) and the OP both confirmed this combo. The OP measured
**+6-8 t/s** on code generation after enabling `ngram-mod` alongside `draft-mtp`.

We currently run `draft-mtp` only with `--spec-draft-n-max 1`. ngram-mod is CPU-side and
free (it matches repeated n-grams already present in the prompt, which is exactly what
agentic loops with lots of repeated structure produce), and it stacks with MTP.

Proposed change to `llama_cmd` in `run.py`:

```
"--spec-type", "ngram-mod,draft-mtp",
"--spec-draft-n-max", "2",
"--spec-ngram-mod-n-match", "24",
"--spec-ngram-mod-n-min", "48",
"--spec-ngram-mod-n-max", "64",
```

Notes:
- Bumping `--spec-draft-n-max` 1 -> 2 is part of this change; the OP uses n-max=2.
- A/B against the current `draft-mtp` n-max=1 baseline; watch `llama_server.log` for
  the new `tg` / `tg_3s` and `draft acceptance` figures.

### 2. Fix the `--context-shift` no-op — MEDIUM impact, needs an A/B

`llama_server.log` currently logs:

> KV cache shifting is not supported for this context, disabling KV cache shifting

So the `--context-shift` flag we added is being ignored. The likely cause is
`n_parallel=2` with `kv_unified=false`. The OP runs `n_parallel=1` and has
`context-shift=1` working.

If the agent runs strictly serially (one request at a time), dropping to `n_parallel=1`
would (a) enable context-shift and (b) free a full slot's KV for the prompt cache.

Proposed experiment:
- Set `n_parallel: 1` in `repo_config.yaml` and re-check the log for the
  "KV cache shifting" line.
- A/B prefill/decode t/s and prompt-cache reuse before/after.

### 3. Verify / complete reasoning preservation — MEDIUM impact, verify first

The OP uses `chat-template-kwargs = {"preserve_thinking": true, "reasoning_effort": "medium"}`.
We added the `--reasoning-preserve` flag, but the chat template may also expect the
`preserve_thinking` keyword argument to actually retain the trace.

Action:
- Confirm whether `--reasoning-preserve` alone is taking effect.
- If not, add `"preserve_thinking": true` to our existing `--chat-template-kwargs`
  (currently `{"reasoning_effort": "medium"}`).

### 4. `--threads-batch` for prefill — LOW impact, free knob

The OP separates decode threads (`threads=3`) from prefill threads (`threads-batch=4`).
We do not set `--threads-batch`. We have a much stronger CPU than the OP, so this is
minor, but it is a free knob to try if prefill is ever CPU-bound.

Proposed experiment: add `--threads-batch 4` (or tune) and check prefill t/s.

## What we deliberately will NOT copy

- **top_k=15** (vs our 20) — marginal, no clear perf benefit.
- **Smaller batches (1024/512)** — that is a 16GB VRAM constraint; our 4096/4096 is
  fine and faster on a 5090.
- **q4_1 KV cache** — we are at q8_0 with headroom; downgrading would hurt quality for
  no gain.
- **Q3 model** — we run Q4_K_XL; keep it.

## Suggested order of work

1. Apply Recommendation 1 (ngram-mod + MTP, n-max=2). Restart, capture t/s.
2. A/B Recommendation 2 (n_parallel=1 for context-shift + KV reuse).
3. Verify Recommendation 3 (reasoning preservation) — confirm the flag is effective.
4. Optional: try Recommendation 4 (`--threads-batch`) if prefill is ever a bottleneck.
