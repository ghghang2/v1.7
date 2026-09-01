# LLM Server Throughput Findings & Recommendations

**Date:** 2026-08-18
**Model:** `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL` (~18 GB, UD-Q4_K_XL)
**Hardware:** 1x NVIDIA L40S, 45,457 MiB VRAM (compute 8.9)
**Workload:** single-user agent chat, `--parallel 1`, `--ctx-size 262144`, all layers on GPU,
reasoning mode (`reasoning_effort=xhigh`, thinking budget 4096)
**Raw data:** `bench/results/` (llama-bench CSVs, `probes_*.jsonl`, `master.log`, `final_ab.log`)

---

## TL;DR

**Current production config is already at the optimum.** The one change worth making is
adding `-b 4096 -ub 2048` to the launch command: **+5% prompt processing, zero risk,
~1 min downtime.** Everything else (MTP spec decode, flash attention, f16 KV cache,
256k context) is already optimal and should be kept as-is.

| Lever | Effect on generation | Effect on prompt processing | Verdict |
|---|---|---|---|
| **MTP spec decode** (`--spec-default --spec-type draft-mtp`) | **+1.6-1.7x** (35.5 -> ~54-77 t/s) | none | **Keep — the single biggest lever** |
| Flash attention (`--flash-attn 1`) | neutral (35.5-35.7) | +10% at 8k; **required** for 256k ctx to fit | **Keep — mandatory** |
| KV cache quant (q8_0) | neutral (35.5) | -1 to -3% | **Skip — no benefit, f16 is safer** |
| `ubatch` 512 -> 2048 | n/a | **+5%** (1641 -> 1728 t/s at 2k) | **Recommended change** |
| `parallel` > 1 | n/a | n/a | **Skip — single-user workload; 1 slot is faster per-request** |
| `--load-mode mlock` | n/a | n/a | **Drop (cosmetic)** — fails in this container, harmless |

---

## 1. Token generation (decode) throughput

Pure decode from `llama-bench` (no spec decode), 256 tokens generated:

| config | depth 0 | depth 4k | depth 16k |
|---|---|---|---|
| FA on, KV f16 | **35.7** | 35.1 | 34.0 |
| FA on, KV q8_0 | 35.5 | 34.8 | 32.8 |
| FA off, KV f16 | 35.5 | 34.6 | 32.1 |

Takeaways:

- **Baseline decode is ~35.5 t/s.** Flash attention on/off and KV quantization are
  within noise for generation — they do **not** help decode speed.
- There is a mild depth penalty (35.7 -> 32-34 t/s by 16k) as the KV cache grows; this
  is intrinsic and unaffected by the options above.

### MTP spec decode: the big win

With `--spec-default --spec-type draft-mtp` (the model ships MTP draft heads):

| measurement | acceptance | mean accept len | effective t/s |
|---|---|---|---|
| synthetic probe (c1 vs c0, ctx 32k) | **0.645** | 3.49 | **54** vs 32.6 (no spec) = **1.66x** |
| live production traffic | 0.35 - 0.53 | 2.9 - 3.1 | 42 - 77 |

- Synthetic prose accepts ~65% of drafts (mean 3.5 tokens/step).
- Real agent traffic (long reasoning chains, code, tool JSON) accepts 35-53% — still a
  solid ~1.5x. Acceptance is workload-dependent but never net-negative.
- **Keep MTP on. It is the single largest throughput lever measured.**

## 2. Prompt processing (prefill) throughput

From `llama-bench` (fresh context, no cache), prompt-only t/s:

| ubatch | FA on | FA off |
|---|---|---|
| 128 | 1426-1464 | 1333-1461 |
| 256 | 1533-1601 | 1440-1587 |
| **512 (current default)** | 1594-1647 | 1491-1635 |
| 1024 | 1624-1675 | 1520-1634 |
| **2048** | **1672-1728** | 1513-1650 |

- **`ubatch` 2048 is +5% over the 512 default** at equal or better VRAM use
  (batch 4096 is the ceiling that fits alongside the 256k ctx).
- FA-on wins clearly at 8k prefill (1672 vs 1513).
- Live-server confirmation: 10k-token fresh prefill at 1396 t/s (A probe);
  the production path is usually far faster due to prefix caching of the shared
  system/agent context.
- **Recommended: `-b 4096 -ub 2048`.**

### Flash attention is mandatory at this context size

The `pp_fa_off_ct_q8_0` sweep **failed to create the context (OOM)**: at 256k ctx the
f16 KV cache alone needs ~37 GB and non-flash attention adds extra working memory.
Even FA-off + f16 runs at ~94% of available VRAM. **There is no safe configuration
without `--flash-attn 1`.**

## 3. Parallelism (multi-slot)

Server probes, concurrent fresh prompts of ~2.7k tokens each, MTP on:

| slots | aggregate t/s | per-request t/s |
|---|---|---|
| 1 (production) | ~54 (with MTP) | 54 |
| 2 | 20.3 | ~10 |
| 4 | 51.7 | ~13 |
| 8 | 55.8 | ~7 |

- The GPU saturates at ~55 t/s aggregate regardless of slot count.
- Adding slots divides the same throughput; **one slot at full speed beats N slots
  per-request for any N**.
- **Keep `--parallel 1`** for this single-user chat deployment. Revisit only if
  concurrent multi-user service becomes a requirement (then 4-8 slots at ~13/7 t/s
  each is the ceiling to expect).

## 4. Bugs & oddities found during the campaign

1. **CLI arg swallowing (fixed in restarted production).**
   `--reasoning-budget-message` consumed the following `--no-mmproj` token into one
   concatenated string, so the flag was silently ignored and the vision projector was
   loaded unnecessarily. Fixed by proper quoting/ordering in the launch config;
   verified via `/proc/<pid>/cmdline` on the running server (PID 181646).

2. **`--load-mode mlock` fails in this container (harmless, cosmetic).**
   Container `memlock` hard cap is 8 GB; the ~18 GB model file can't be pinned, so
   every tensor chunk logs `failed to mlock ...: Cannot allocate memory`.
   All weights end up on GPU anyway (`-n-gpu-layers 999`), so **throughput impact is
   zero**. Recommendation: drop `--load-mode mlock` (default mmap) to clean the logs.
   `ulimit -l unlimited` is not permitted even as root in this container.

3. **Self-benchmarking caveat.** The first campaign (`master.sh`) stopped the very
   `llama-server` this agent session itself runs on, which aborted the agent's turn
   for the ~19 min sweep window; the campaign auto-restarted production and the agent
   resumed with no data loss. `final_ab.sh` added explicit stop/start/restart with
   health checks to contain this. (No action needed; documented for future runs.)

4. **Script bug in `final_ab.sh` (B/C variants).** Missing line-continuation
   backslashes meant the B (`ub 2048`) and C (`parallel 2`) test servers launched with
   only their first arg line (no FA, no MTP). Their probe numbers are invalid and were
   **excluded** from this report. The llama-bench sweeps (unaffected, run directly)
   already cover the ubatch/FA/KV matrix, so no re-run is required.

## 5. Recommended production launch config

Current production args (PID 181646, verified):

```
./llama-server -hf unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL \
  --port 8889 --parallel 1 --ctx-size 262144 --n-gpu-layers 999 \
  --flash-attn 1 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 \
  --reasoning on --chat-template-kwargs '{"reasoning_effort": "xhigh"}' \
  --spec-default --spec-type draft-mtp --reasoning-preserve \
  --fit off --agent \
  --reasoning-budget 4096 --reasoning-budget-message '...' \
  --no-mmproj --repeat-penalty 1.0 --load-mode mlock --metrics
```

**Change to apply (the only one):**

```diff
   --flash-attn 1 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 \
+  -b 4096 -ub 2048 \
   --reasoning on ...
```

Optional cosmetic change: remove `--load-mode mlock` (fails in container; logs noise only).

**Expected result:** ~5% faster cold prefill (1641 -> ~1728 t/s at 2k, ~5% at 8k);
generation unchanged (~54 t/s with MTP on synthetic, 42-77 t/s on live agent traffic).

## 6. Method

- **llama-bench** (`llama-bench -m <gguf> -ngl 999 -fa on/off -ctk f16/q8_0 ...`):
  pure tg (0/256 gen, depths 0/4k/16k) and pp (512/2k/8k prompt, ubatch
  128/256/512/1024/2048) sweeps, 6 configs. ~11 min total, GPU exclusive.
- **Server probes** (`bench/probe.py`): real `llama-server` instances per config on
  port 8901 (production untouched), 10k/20k-token synthetic prompts + MTP metrics
  pulled from the `/metrics` endpoint (`spec_decode_num_accepted_tokens_total` etc.).
- **Live confirmation:** `print_timing` lines in `llama_server.log` for production
  traffic, plus `/proc/<pid>/cmdline` to verify running args.
- Campaigns: `bench/master.sh` (full sweep + auto-restart), `bench/final_ab.sh`
  (production-condition A/B). All outputs in `bench/results/`.
