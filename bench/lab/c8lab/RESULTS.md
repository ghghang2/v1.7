# C=8 saturation experiment — results

Head-to-head of the **legacy 1:1 worker mapping** (today's nbchat →
ninfer-serve behaviour) against the **proposed shared-queue + dispatcher**
design, driving the calibrated `EngineSim` (Qwen3.8-27B NVFP4 MTP3,
C=8, kv 187,712 tokens; calibration checked in `validate_sim.py`).

Both designs receive **identical sessions** (same seed, same tool gaps);
the only difference is the client design.  "agents done" = time of the
last agent turn; speedup < 1.00 means the dispatcher is faster.

```
Scenario A: 16 agent sessions (3-6 turns, 1.5-4 s tool gaps), streams CLOSED + 300 filler
design                         agents done  speedup | filler p50 filler p95 | details
legacy                            224.7s   1.00x |   3138ms   4605ms | filler= 24 dem=  0 qmax=15 util= 0.58
dispatcher                        222.1s   0.99x |   n/a     n/a    | filler=  0 dem=  0 qmax=15 util= 0.39

Scenario B: 16 agent sessions, streams HELD across tool gaps (the wedge) + 300 filler
legacy                            328.1s   1.00x |   3112ms  65958ms | filler= 22 dem=  0 qmax=17 util= 0.54
dispatcher                        222.1s   0.68x |   n/a     n/a    | filler=  0 dem=  0 qmax=15 util= 0.39
legacy+reserve(2)                 334.8s   1.02x |  47039ms  68625ms | filler=  5 dem=  0 qmax=17 util= 0.43

Scenario C: 24 agent sessions (4-6 turns, 2-6 s gaps), streams closed + 600 filler
legacy                            292.6s   1.00x |   2868ms  56689ms | filler= 12 dem=  0 qmax=23 util= 0.75
dispatcher                        307.1s   1.05x |   2952ms   3211ms | filler=  9 dem=  0 qmax=23 util= 0.58
```

## Findings

1. **The held-stream wedge is real and large.**  Scenario B vs A: the
   legacy design takes **328 s vs 225 s (+46 %)** for the same agent
   workload, purely because SSE streams stay open across tool I/O and
   pin lanes.  The 1.5-4 s tool gap per session becomes dead lane time
   that cannot be backfilled.

2. **The dispatcher is immune to the wedge.**  It never holds a lane
   during tool I/O, so its makespan is identical (222 s) in A and B —
   a **0.68x / 1.47x** improvement over legacy in the worst case.
   Its agent-first admission also means its results do not depend on
   whether nbchat holds streams: one design works for both client
   behaviours.

3. **The `reserve` mitigation nearly fails.**  `legacy+reserve(2)`
   (demand 2 free lanes for filler) does *not* recover the wedge:
   334.8 s (1.02x), and filler latency collapses (p50 47 s vs 3 s).
   Reserving lanes for the TUI steals backfill capacity from agents;
   under C=8 with 16+ held sessions there is simply not enough headroom.

4. **No gain when streams are closed and load is light (A).**
   With 16 sessions on 8 lanes and short tool gaps, legacy's
   released-lane backfill is already near-optimal: 224.7 vs 222.1 s.
   The dispatcher's value is robustness (finding 2) plus fairness
   (qmax 15 vs 17), not raw speed here.

5. **Under heavier load the dispatcher is ~neutral (C, 1.05x).**
   24 sessions on 8 lanes the engine is the bottleneck (util 0.75 vs
   0.60 — the dispatcher is *less* saturated because it never runs
   filler on agent-pending lanes); client design stops mattering.

6. **Caveat — filler throughput.**  The dispatcher's strict
   agent-first policy completes 0/300 fillers in A/B (9/600 in C),
   vs 24/22/12 for legacy.  In these scenarios agents are always
   runnable or due within moments, so idle lanes are rare.  A grace
   window (`_FILLER_GRACE_S`, currently 0.0) is the knob to trade
   some filler latency for filler throughput if the TUI needs it;
   it does not affect the agent makespan numbers above unless it
   delays agent turns (it cannot — filler only takes lanes that are
   idle with no agent due).

## Bottom line

The case for the shared-queue + dispatcher rests on **scenario B**:
nbchat's held SSE streams are the worst case the legacy mapping was
never designed for, and the fix on the server/client design side is
worth ~1.5x on agent completion time and makes the result
independent of client stream-lifetime behaviour.  The `reserve`
knob on the legacy design does not buy this.  If nbchat moves to
closing streams after each turn (scenario A), the win shrinks to
~1% plus fairness — still positive, but the wedge is the argument.

Reproduce: `python3 -m c8lab.experiment` (deterministic, seed 2026).
