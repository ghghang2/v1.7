#!/usr/bin/env python3
"""Validate the engine simulator against the published corpus results.

Run:  python3 -m c8lab.validate_sim   (from /v1.7)

Compares the simulated LegacyClient (the published runner's exact
pattern: C workers, submit-on-completion) against the measured
rows in workspace/ninfer/docs/performance.md for Qwen3.8-27B NVFP4
MTP3, 75-request corpus, --kv-capacity auto:

    C  makespan (s)  decode tok/s  avg batch
    1  4,670.27      161.1         1.00
    2  2,510.78      294.7         1.98
    4  1,647.74      432.9         3.29
    8  2,164.90      334.2         2.36
"""

from __future__ import annotations

import sys

from .sim import EngineSim, ServerConfig
from .clients import LegacyClient
from .workload import corpus_workload

PUBLISHED = {
    1: (4670.27, 161.1, 1.00, 752_160),
    2: (2510.78, 294.7, 1.98, 739_951),
    4: (1647.74, 432.9, 3.29, 713_384),
    8: (2164.90, 334.2, 2.36, 723_602),
}


def main() -> int:
    tasks = corpus_workload()
    print(f"corpus: {len(tasks)} items, "
          f"prefill={sum(t.prompt_tokens for t in tasks):,} tokens, "
          f"target decode={sum(t.target_tokens for t in tasks):,} tokens")
    print()
    hdr = f"{'C':>2} {'makespan':>10} {'pub':>10} {'d%':>7} | " \
          f"{'dec tok/s':>9} {'pub':>8} {'d%':>7} | {'degrad':>7}"
    print(hdr)
    ok = True
    for C in (1, 2, 4, 8):
        sim = EngineSim(ServerConfig.nvfp4(C))
        client = LegacyClient(sim, tasks, C)
        makespan = client.run()
        done = [r for r in sim.results if r.ok]
        dec_tok = sum(r.produced for r in done)
        tps = dec_tok / makespan
        pub_ms, pub_tps, pub_batch, pub_dec = PUBLISHED[C]
        d_ms = 100.0 * (makespan - pub_ms) / pub_ms
        d_tps = 100.0 * (tps - pub_tps) / pub_tps
        if abs(d_ms) > 25 or abs(d_tps) > 25:
            ok = False
        print(f"{C:>2} {makespan:>10.0f} {pub_ms:>10.0f} {d_ms:>6.1f}% | "
              f"{tps:>9.1f} {pub_tps:>8.1f} {d_tps:>6.1f}% | "
              f"{sim.degradations:>7}  (decode {dec_tok:,} vs pub "
              f"{pub_dec:,})")
    print()
    print("PASS: simulator within 25% of measured makespan and decode rate"
          if ok else
          "FAIL: simulator drifts >25% from a measured row")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
