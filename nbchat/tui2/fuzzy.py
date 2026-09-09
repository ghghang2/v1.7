"""Fuzzy subsequence matching for the TUI v2 (Phase 3).

Used by selector lists and command autocomplete: ``fuzzy_match``
reports whether *needle* is a subsequence of *haystack*, with a score
used to rank candidates.  Longer, earlier, and boundary-aligned
matches score higher.

Pure functions — no I/O, no terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class FuzzyMatch:
    """A match result: the *score* (higher is better) and the
    *indices* of the matched characters inside *haystack* (useful for
    highlighting in a selector)."""
    score: float
    indices: List[int]


def fuzzy_match(needle: str, haystack: str) -> Optional[FuzzyMatch]:
    """Return the best :class:`FuzzyMatch` or ``None`` when *needle*
    is not a subsequence of *haystack* (case-insensitive).

    Scoring:

    - ``+2.0`` per matched character
    - ``+3.0`` bonus for a match at index 0 (word-start prefix)
    - ``+1.0`` bonus when the previous character is a word boundary
      (``_``, ``-``, space) — i.e. the character starts a word
    - ``-0.5 * gap`` penalty for skipped characters between matches
      (capped at ``-4.0`` per gap)
    """
    if not needle:
        return FuzzyMatch(0.0, [])
    n = needle.lower()
    h = haystack.lower()
    if not h:
        return None

    best: Optional[FuzzyMatch] = None
    # Memoised search over (n_i, h_i) states.
    memo: dict = {}

    def search(ni: int, hi: int) -> Optional[tuple]:
        """Return (score, indices_from_here) best from this state."""
        if ni == len(n):
            return (0.0, [])
        if (ni, hi) in memo:
            return memo[(ni, hi)]
        best_here: Optional[tuple] = None
        for j in range(hi, len(h)):
            if h[j] == n[ni]:
                rest = search(ni + 1, j + 1)
                if rest is None:
                    continue
                rest_score, rest_idx = rest
                gap = j - hi
                score = 2.0 - min(4.0, 0.5 * gap) + rest_score
                if j == 0:
                    score += 3.0
                elif j > 0 and not h[j - 1].isalnum():
                    score += 1.0
                cand = (score, [j] + rest_idx)
                if best_here is None or cand[0] > best_here[0]:
                    best_here = cand
        memo[(ni, hi)] = best_here
        return best_here

    result = search(0, 0)
    if result is None:
        return None
    return FuzzyMatch(result[0], result[1])


def fuzzy_rank(needle: str, items: Sequence[str],
               limit: int = 0) -> List[tuple]:
    """Rank *items* by fuzzy score against *needle*, best first.

    Returns ``(item, match)`` pairs; items with no match are dropped.
    ``limit`` caps the number of results (0 = no cap).  Ties break in
    original order (stable).
    """
    scored: List[tuple] = []
    for item in items:
        m = fuzzy_match(needle, item)
        if m is not None:
            scored.append((item, m))
    scored.sort(key=lambda pair: -pair[1].score)
    return scored[:limit] if limit else scored
