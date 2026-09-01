#!/usr/bin/env python3
"""
log_report.py - quick throughput / latency report for the llama-server logs.

Run after each config change to verify performance gains:

    python log_report.py            # uses repo_config.yaml paths
    python log_report.py my.log     # or point at a specific llama_server.log

It prints:
  * aggregate DECODE rate (t/s) over all completed tasks
  * per-task decode t/s distribution (min/med/max)
  * aggregate PREFILL rate (t/s) for prompts > 1000 tokens
  * TTFT stats from inference_metrics.log (if present)
  * counts of the two warnings that signal perf problems:
      - "exceeds cache size limit ... skipping"  (prefix reuse disabled)
      - "failed to mlock"                        (RLIMIT_MEMLOCK too low)

After applying the tuned run.py, the "cache size limit" count should drop
to ~0 and TTFT on large requests should fall dramatically.
"""

from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

# --- locate the logs ------------------------------------------------------- #
def _paths(argv: list[str]) -> tuple[Path, Path | None]:
    if len(argv) > 1:
        log = Path(argv[1])
        return log, log.parent / "inference_metrics.log"
    try:
        from nbchat.core import config
        log = Path(config.LLAMA_LOG_PATH)
        return log, Path("inference_metrics.log")
    except Exception:
        return Path("llama_server.log"), Path("inference_metrics.log")


RE_DECODE = re.compile(r"(?<!prompt )eval time =\s+([0-9.]+) ms /\s+([0-9]+) tokens")
RE_PROMPT = re.compile(r"prompt eval time =\s+([0-9.]+) ms /\s+([0-9]+) tokens")
RE_TTFT = re.compile(r"TTFT:\s+([0-9.]+)s")


def _dist(vals: list[float]) -> str:
    if not vals:
        return "n/a"
    s = sorted(vals)
    med = statistics.median(s)
    return f"min={s[0]:.1f} med={med:.1f} max={s[-1]:.1f} (n={len(s)})"


def main(argv: list[str]) -> int:
    llama_log, metrics_log = _paths(argv)
    if not llama_log.exists():
        print(f"[log_report] {llama_log} not found")
        return 1

    txt = llama_log.read_text(encoding="utf-8", errors="replace")

    # --- decode (generation) rate ----------------------------------------- #
    decode_rates, total_gen, total_decode_ms = [], 0, 0.0
    for ms, n in RE_DECODE.findall(txt):
        ms, n = float(ms), int(n)
        total_gen += n
        total_decode_ms += ms
        if n > 0:
            decode_rates.append(n / (ms / 1000.0))

    # --- prefill (prompt) rate, large prompts only ------------------------ #
    prefill_rates, total_pref_ms = [], 0.0
    for ms, n in RE_PROMPT.findall(txt):
        ms, n = float(ms), int(n)
        total_pref_ms += ms
        if n >= 1000:
            prefill_rates.append(n / (ms / 1000.0))

    # --- TTFT from metrics log -------------------------------------------- #
    ttft = []
    if metrics_log and metrics_log.exists():
        mtxt = metrics_log.read_text(encoding="utf-8", errors="replace")
        ttft = [float(x) for x in RE_TTFT.findall(mtxt)]

    # --- problem-warning counts ------------------------------------------- #
    cache_skip = txt.count("exceeds cache size limit")
    mlock_fail = txt.count("failed to mlock")

    print("=" * 62)
    print(f"Log: {llama_log}")
    print("=" * 62)
    if total_decode_ms > 0:
        print(f"DECODE  : {total_gen:,} tokens in {total_decode_ms/1000:.1f} s "
              f"= {total_gen/(total_decode_ms/1000.0):.1f} t/s overall")
        print(f"          per-task t/s: {_dist(decode_rates)}")
    else:
        print("DECODE  : no completed eval-time lines found")

    if total_pref_ms > 0:
        print(f"PREFILL : {len(prefill_rates)} large prompts (>1k tok), "
              f"t/s: {_dist(prefill_rates)}")

    if ttft:
        print(f"TTFT    : n={len(ttft)} "
              f"min={min(ttft):.2f} med={statistics.median(ttft):.2f} "
              f"max={max(ttft):.2f} s")

    print("-" * 62)
    print(f"WARNINGS  : cache-reuse skips = {cache_skip}   "
          f"mlock failures = {mlock_fail}")
    if cache_skip:
        print("            ^ >0 means prefix/KV reuse is being skipped -> "
              "full re-prefill each turn (raise --cache-ram / q8_0 KV).")
    if mlock_fail:
        print("            ^ mlock failed -> raise RLIMIT_MEMLOCK or use "
              "--load-mode mmap.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
