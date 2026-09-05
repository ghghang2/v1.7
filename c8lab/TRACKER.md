# C=8 saturation experiment — tracker

## State: COMPLETE — committed 28bbf80 and pushed to main (299 tests pass)

## Final steps (all done)
- [x] RESULTS.md written (table + 6 findings + bottom line)
- [x] run_tests: 299 passed
- [x] Committed (28bbf80) and pushed: all c8lab/ files on main

## Done (do NOT repeat)
  - Fairness fix: both designs get IDENTICAL sessions (same seed);
    dead `if False` code removed from _scenario
  - Verified: all workload RNGs are locally seeded (deterministic);
    dispatcher rows for A/B are identical (222.1s) AS EXPECTED —
    dispatcher never holds lanes, so held_gap makes no difference to it
  - Dispatcher filler=0 in A/B is a REAL consequence of the agent-first
    policy (16 sessions, 8 lanes: an idle lane with no agent due is
    rare), NOT a bug. C gets 9. Noted in RESULTS.md.
- [x] `c8lab/sim.py` — calibrated EngineSim (kv 187,712, C=8, MTP3 NVFP4)
- [x] `c8lab/workload.py` — agent sessions + filler workload
- [x] `c8lab/clients.py` — legacy 1:1 + dispatcher clients
  - Bug fixed: dispatcher didn't re-arm due-heap after a turn (turn 2+ never ran)
  - Bug fixed: filler policy treated in-flight sessions as "due" → 0 fillers;
    in-flight sids now excluded; grace = 0.0
- [x] `c8lab/experiment.py` — 3 scenarios A/B/C, head-to-head table
- [x] `c8lab/validate_sim.py` — calibration check
- [x] Earlier bug: mangled patch left mis-indented lines in sim.py `__init__` — fixed
- [x] One clean end-to-end run achieved (table prints, all 3 scenarios)

## Outstanding (in order)
1. [x] Rerun done (last run above): A 0.99x, B 0.68x (reserve(2) 1.02x), C 1.05x
2. [ ] Write `c8lab/RESULTS.md` with the table + honest reading
3. [ ] run_tests, then commit c8lab/ + push

## Final numbers (seed 2026, identical sessions both designs)
- A (closed streams):  legacy 224.7s | dispatcher 222.1s (0.99x)
- B (held streams):    legacy 328.1s | dispatcher 222.1s (0.68x) |
  legacy+reserve(2) 334.8s (1.02x, filler p50 47s — mitigation nearly useless)
- C (24 sessions):     legacy 292.6s | dispatcher 307.1s (1.05x)

## Notes / gotchas
- `make_change_to_file` diffs must match current file content exactly — several
  "Invalid Context" failures happened; always sed the region first.
- experiment.py `_scenario` has dead `if False` code — clean up in fix 2.
- Budget is low: no refactoring, minimal reruns.
