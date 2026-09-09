"""Workload definitions for the C=8 saturation experiments."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List


@dataclass
class WorkItem:
    kind: str              # "agent" | "filler"
    prompt_tokens: int
    target_tokens: int
    context_ceiling: int
    due: float = 0.0


def corpus_workload(seed: int = 20260811, n_agents: int = 75) -> List[WorkItem]:
    """Approximation of the speculative-decode corpus (75 requests).

    Fitted to the published tables (docs/performance.md, nvfp4 corpus
    2026-08-17): computed prefill 15,460 tokens; decode 752,160;
    long-reasoning rendered prompt 293 tokens with per-fixture
    completions ~10.7K / 47.3K / 61.6K (one run hits the 65,536
    budget); cross-scenario prompts ~200 tokens, 4,096-token output
    limit, completions ~2.5K:
      prefill = 15*293 + 60*200      ~= 16,400  (pub 15,460)
      decode  = 5*(10.7+47.3+61.6)K  ~= 556K
                + 60*2.4K            ~= 144K  -> ~700K (pub 752K)
    """
    rng = random.Random(seed)
    items: List[WorkItem] = []
    long_shapes = [10_700, 47_300, 61_600]
    for i in range(15):
        shape = long_shapes[i % 3]
        if i == 14:  # one run saturates the 65,536 budget
            shape = 65_536
        items.append(WorkItem("agent", rng.randint(280, 310),
                              shape, 65_536))
    for i in range(60):
        items.append(WorkItem("agent", rng.randint(150, 250),
                              rng.randint(2_000, 2_900), 4_096))
    rng.shuffle(items)
    return items[:n_agents]


def filler_workload(seed: int = 7, n: int = 10_000) -> List[WorkItem]:
    """Short TUI turns: ~1.5K prompt, ~350-token answers."""
    rng = random.Random(seed)
    return [WorkItem("filler", rng.randint(1_000, 2_200),
                     rng.randint(250, 450), 8_192) for _ in range(n)]


def agent_plus_tool_workload(seed: int = 11, n_agents: int = 24,
                             tool_gap_s: float = 2.0) -> List[WorkItem]:
    """24 long-horizon agent tasks (the nbchat scenario)."""
    return corpus_workload(seed=seed, n_agents=n_agents)
