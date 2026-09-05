"""Head-to-head: legacy 1:1 worker mapping vs. the proposed shared
queue + dispatcher, driving the calibrated C=8 EngineSim.

Scenarios (nbchat-shaped multi-turn agent sessions + TUI filler turns):

  A  16 sessions, streams closed between turns (engine may backfill)
  B  16 sessions, SSE streams HELD across tool gaps (the wedge)
  C  24 sessions (full nbchat scale), streams closed

Per scenario we report: agent makespan (last agent turn complete),
speedup vs. legacy, filler p50/p95 latency, filler completed, engine
KV degradations, prefill-queue depth, and mean lane utilization.
"""

from __future__ import annotations

import random
from typing import List

from .clients import AgentSession, MultiTurnClient
from .sim import EngineSim, ServerConfig
from .workload import filler_workload


def _pct(values: List[float], q: float) -> str:
    if not values:
        return "  n/a  "
    v = sorted(values)
    return f"{v[min(len(v) - 1, int(q * len(v)))] * 1000:6.0f}ms"


def _sessions(rng: random.Random, n: int, turns: tuple, gaps: tuple,
              held: bool) -> List[AgentSession]:
    return [
        AgentSession(
            sid=i,
            total_turns=rng.randint(*turns),
            prompt_tokens=rng.randint(8_000, 14_000),
            turn_tokens=rng.randint(800, 2_500),
            tool_gap=rng.uniform(*gaps),
            ceiling=16_384,
            held_gap=held,
        )
        for i in range(n)
    ]


def _row(label: str, r: dict, base: dict | None = None) -> str:
    sp = "" if base is None else f"{r['t_agents'] / base['t_agents']:6.2f}x"
    fp50 = _pct(r["filler_latencies"], 0.50)
    fp95 = _pct(r["filler_latencies"], 0.95)
    det = (f"filler={r['filler_done']:3d} dem={r['degradations']:3d} "
           f"qmax={r['max_pending']} util={r['util']:5.2f} "
           f"first={r['first_agent']:6.1f}s")
    return (f"{label:30s} {r['t_agents']:8.1f}s {sp} | "
            f"{fp50:>7s} {fp95:>7s} | {det}")


def _run(mode: str, sessions: List[AgentSession],
         n_fillers: int, seed: int, reserve: int = 0) -> dict:
    sim = EngineSim(ServerConfig.nvfp4(8), rng=random.Random(seed))
    fillers = filler_workload(seed=seed + 1, n=n_fillers)
    client = MultiTurnClient(sim, sessions, mode, fillers=fillers,
                             reserve=reserve)
    t_agents = client.run()
    span = max(t_agents, 1e-9)
    util = (sum(u for _t, u in client._util) / len(client._util)) / 8 \
        if client._util else 0.0
    return {
        "t_agents": t_agents,
        "first_agent": client.first_done_time or 0.0,
        "filler_done": client.filler_done,
        "filler_latencies": client.filler_latencies,
        "degradations": sim.degradations,
        "max_pending": sim.max_pending_depth,
        "util": util,
        "wall_s": span,
    }


def _scenario(title: str, n_agents: int, turns: tuple, gaps: tuple,
              held: bool, n_fillers: int, seed: int) -> None:
    print(f"\n{title}")
    runs = []
    for mode in ("legacy", "dispatcher"):
        # identical sessions for both designs (same seed); only the
        # held-stream behaviour differs (dispatcher never holds)
        sessions = _sessions(random.Random(seed), n_agents, turns, gaps,
                             held and mode == "legacy")
        runs.append((mode, _run(mode, sessions, n_fillers, seed)))
    if held:
        # mitigation: legacy but filler must leave 2 lanes free
        sessions = _sessions(random.Random(seed), n_agents, turns, gaps, True)
        runs.append(("legacy+reserve(2)",
                     _run("legacy", sessions, n_fillers, seed, reserve=2)))
    print(f"{'design':30s} {'agents done':>9s}  {'speedup':>6s} | "
          f"{'filler p50':>7s} {'filler p95':>7s} | details")
    base = runs[0][1]
    for mode, r in runs:
        print(_row(mode, r, base))
    for mode, r in runs:
        print(f"    {mode:28s} sim wall-clock {r['t_agents']:7.1f}s "
              f"({r['wall_s']:7.1f}s sim time)")


def main() -> int:
    cfg = ServerConfig.nvfp4(8)
    print(f"Qwen3.8-27B NVFP4 MTP3, C={cfg.C}, kv {cfg.kv_tokens:,} tokens "
          f"(validated sim)")
    _scenario("Scenario A: 16 agent sessions (3-6 turns, 1.5-4 s tool "
              "gaps), streams CLOSED + 300 filler",
              16, (3, 6), (1.5, 4.0), False, 300, seed=2026)
    _scenario("Scenario B: 16 agent sessions, streams HELD across tool "
              "gaps (the wedge) + 300 filler",
              16, (3, 6), (1.5, 4.0), True, 300, seed=2026)
    _scenario("Scenario C: 24 agent sessions (4-6 turns, 2-6 s gaps), "
              "streams closed + 600 filler",
              24, (4, 6), (2.0, 6.0), False, 600, seed=2026)
    print("\nnote: 'agents done' is the time of the last agent turn; "
          "speedup is relative to the scenario's legacy baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
