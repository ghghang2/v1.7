"""Continual-harness refinement for nbchat (port of prime-agent /refine).

What this is
------------
prime-agent's refinement feature (``refine/impl.ts``:
``reviewAutoRefine`` + ``applyRefineEdits``) reviews the agent's own
harness — its memory and accumulated lessons — *between* tasks and
emits precise create/update/delete edits that the deterministic runtime
executor applies, with an audit trail and rollback.

This module is the nbchat port.  The LLM half (prompt + JSON parsing)
is isolated from the deterministic half (sanitise, dedup, apply, audit,
rollback) so the risky part is testable with scripted responses.

Design invariants
-----------------
* The LLM never writes to the DB directly.  It returns a JSON object;
  the executor decides what to apply and logs everything.
* Global-scope ``delete`` is refused (lessons are cross-session memory;
  a scoped review must not erase it silently).
* Similarity dedup: a proposed lesson that closely matches an existing
  one is an *update* of that lesson (useful counter preserved), never a
  duplicate row — refinement strengthens memory instead of re-learning
  it (prime-agent keeps ``learnedFrom`` for the same reason).
* Every refine round is one ``refinement_history`` row (kind='refine',
  round_id=N) plus per-edit rows; :func:`undo_last_round` reverts the
  round from that audit trail alone.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from nbchat.core import db

_log = logging.getLogger("nbchat.refinement")

# ---------------------------------------------------------------------------
# Tunables (module constants; config wiring comes with the daemon phase)
# ---------------------------------------------------------------------------

REFINE_MAX_OUTPUT_TOKENS = 1024
REFINE_MAX_EDITS = 3
REFINE_MAX_CONTENT_CHARS = 400
REFINE_DEDUP_SIMILARITY = 0.85
REFINE_HISTORY_SHOWN = 10


@dataclass
class RefineEdit:
    """One sanitised, executor-ready harness edit."""
    op: str                      # create | update | delete
    content: str = ""
    rationale: str = ""
    scope: str = "session"
    target_id: Optional[int] = None   # update/delete: lesson id


@dataclass
class RefineResult:
    """Outcome of one refine round (LLM or programmatic)."""
    applied: list[RefineEdit] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    round_id: int = 0
    should_refine: bool = True
    scope: str = "session"
    raw: Any = None


# ---------------------------------------------------------------------------
# Review triggers — deterministic, no LLM involved
# ---------------------------------------------------------------------------

def should_refine_task(*, status: str, failed_tools: int = 0,
                       num_tool_turns: int = 0) -> tuple[bool, str]:
    """Decide whether a just-ended task is worth a refinement review.

    Triggers (mirrors prime-agent's auto-refine triggers):
      * task completed (status ``done``) — normal harvest window;
      * task failed or was interrupted — postmortem window;
      * a high tool-failure count within the task (>= 3).
    """
    if status in ("done", "complete"):
        return True, "task completed"
    if status in ("failed", "error", "interrupted"):
        return True, f"task {status}"
    if failed_tools >= 3:
        return True, f"{failed_tools} failed tool calls in task"
    return False, ""


# ---------------------------------------------------------------------------
# Prompt construction (mirrors prime-agent's reviewAutoRefine prompt shape)
# ---------------------------------------------------------------------------

REVIEW_PROMPT = """\
You are nbchat's refinement reviewer (a port of prime-agent's /refine).
Review the harness state for session {session_id} and decide whether it
needs improvement.

[Current session state]
Task: {task_summary}
Recent trajectory:
{trajectory}

[Harness state]
{harness_state}

[Refinement history (last {history_n} rounds)]
{history}

[Existing lessons]
{lessons}

Respond with STRICT JSON only, no prose:
{{
  "should_refine": true/false,
  "scope": "session" | "global",
  "reasoning": "<= 2 sentences, why",
  "edits": [
    {{"op": "create", "content": "<= 400 chars", "rationale": "short",
      "scope": "session" | "global"}},
    {{"op": "update", "id": <lesson_id>, "content": "<= 400 chars",
      "rationale": "short"}},
    {{"op": "delete", "id": <lesson_id>, "rationale": "short"}}
  ]
}}

Rules:
- Max {max_edits} edits. Prefer zero edits over noise.
- "global" scope means the lesson is injected into ALL future sessions;
  use it only for durable, cross-task guidance (tool usage, workflow,
  user preference). Task-specific facts stay "session".
- Never delete a "global" lesson.
- Edits should be precise, self-contained, imperative.
"""


def _harness_state_block(core_memory: dict, lessons: list[dict]) -> str:
    """Compact, display-id-annotated harness overview (prime-agent
    ``formatHarnessStateForPrompt`` equivalent)."""
    lines = ["[core memory]"]
    for k, v in list(core_memory.items())[:12]:
        lines.append(f"  {k}: {str(v)[:120]}")
    lines.append("[lessons] (ids shown in brackets)")
    if not lessons:
        lines.append("  (none)")
    for r in lessons[:30]:
        lines.append(f"  [{r['id']}] ({r['scope']}, useful={r['useful']}) "
                     f"{r['content'][:160]}")
    return "\n".join(lines)


def build_review_prompt(*, session_id: str, task_summary: str,
                        trajectory: str, core_memory: dict,
                        lessons: list[dict],
                        history: list[dict]) -> str:
    hist = "\n".join(
        f"  round {h.get('round_id')}: {h.get('action', '')} "
        f"({h.get('detail', '')[:100]})"
        for h in history[:REFINE_HISTORY_SHOWN]) or "  (none)"
    return REVIEW_PROMPT.format(
        session_id=session_id,
        task_summary=task_summary[:500],
        trajectory=trajectory[:3000],
        harness_state=_harness_state_block(core_memory, lessons),
        history_n=REFINE_HISTORY_SHOWN,
        history=hist,
        lessons=lessons[:30] and "\n".join(
            f"  [{r['id']}] ({r['scope']}) {r['content'][:160]}"
            for r in lessons[:30]) or "  (none)",
        max_edits=REFINE_MAX_EDITS,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class RefineParseError(ValueError):
    """Raised when a reviewer response cannot be parsed as a valid plan."""


def _coerce_edits(obj: Any) -> list[dict]:
    if not isinstance(obj, list):
        raise RefineParseError("edits must be a list")
    out = []
    for e in obj:
        if not isinstance(e, dict):
            raise RefineParseError("each edit must be an object")
        op = str(e.get("op", "")).strip().lower()
        if op not in ("create", "update", "delete"):
            raise RefineParseError(f"bad op: {op!r}")
        out.append(e)
    return out


def parse_refine_response(raw: str) -> dict:
    """Parse a reviewer LLM response into a normalised plan dict.

    Tolerates a single fenced code block wrapper; raises
    :class:`RefineParseError` on anything unrecoverable.
    """
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RefineParseError("no JSON object in response")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RefineParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RefineParseError("top-level JSON must be an object")
    return {
        "should_refine": bool(obj.get("should_refine", True)),
        "scope": obj.get("scope") if obj.get("scope") in ("session", "global")
        else "session",
        "reasoning": str(obj.get("reasoning", ""))[:300],
        "edits": _coerce_edits(obj.get("edits", [])),
    }


# ---------------------------------------------------------------------------
# Deterministic executor
# ---------------------------------------------------------------------------

def _dedup_target(content: str, lessons: list[dict]) -> Optional[dict]:
    """Return the existing lesson most similar to ``content`` above the
    similarity threshold, or None."""
    best, best_ratio = None, 0.0
    for r in lessons:
        ratio = difflib.SequenceMatcher(
            None, content.lower(), r["content"].lower()).ratio()
        if ratio > best_ratio:
            best, best_ratio = r, ratio
    if best_ratio >= REFINE_DEDUP_SIMILARITY:
        return best
    return None


def sanitize_edits(raw_edits: list[dict], lessons: list[dict],
                   default_scope: str = "session") -> tuple[list[RefineEdit], list[str]]:
    """Normalise raw LLM edits into executor-safe :class:`RefineEdit`
    objects.  Returns ``(edits, skip_reasons)``."""
    edits: list[RefineEdit] = []
    skipped: list[str] = []
    existing_ids = {r["id"] for r in lessons}

    for e in raw_edits[:REFINE_MAX_EDITS + 4]:
        if len(edits) >= REFINE_MAX_EDITS:
            skipped.append(f"edit cap reached, dropped: "
                           f"{str(e.get('content', ''))[:60]}")
            continue
        op = str(e.get("op", "")).strip().lower()
        content = re.sub(r"\s+", " ", str(e.get("content", ""))).strip()
        rationale = str(e.get("rationale", "")).strip()[:200]
        scope = e.get("scope") if e.get("scope") in ("session", "global") \
            else default_scope

        if op == "create":
            if not content:
                skipped.append("create with empty content")
                continue
            if len(content) > REFINE_MAX_CONTENT_CHARS:
                content = content[:REFINE_MAX_CONTENT_CHARS].rstrip()
            dup = _dedup_target(content, lessons)
            if dup is not None:
                # Strengthen the existing lesson instead of duplicating.
                merged = (dup["content"] + " | " + content)[:REFINE_MAX_CONTENT_CHARS]
                edits.append(RefineEdit(
                    op="update", target_id=dup["id"], content=merged,
                    rationale=f"dedup into #{dup['id']}: {rationale}",
                    scope=dup["scope"]))
                lessons.append({"id": dup["id"], "content": merged,
                                "scope": dup["scope"], "useful": dup["useful"]})
                continue
            edits.append(RefineEdit(op="create", content=content,
                                    rationale=rationale, scope=scope))
            lessons.append({"id": -len(edits), "content": content,
                            "scope": scope, "useful": 0})  # for dedup within round
        elif op in ("update", "delete"):
            tid = e.get("id")
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                skipped.append(f"{op} with non-integer id: {tid!r}")
                continue
            if tid not in existing_ids:
                skipped.append(f"{op} of unknown lesson id {tid}")
                continue
            if op == "delete" and next(
                    (r for r in lessons if r["id"] == tid), {}).get("scope") \
                    == "global":
                skipped.append(f"refused to delete global lesson #{tid}")
                continue
            if op == "update":
                if not content:
                    skipped.append(f"update #{tid} with empty content")
                    continue
                content = content[:REFINE_MAX_CONTENT_CHARS].rstrip()
                for r in lessons:
                    if r["id"] == tid:
                        r["content"] = content
                        break
            edits.append(RefineEdit(op=op, target_id=tid,
                                    rationale=rationale, content=content))
    return edits, skipped


def apply_edits(session_id: str, edits: list[RefineEdit], *,
                round_id: int, origin: str = "refine") -> tuple[list[int], list[int]]:
    """Apply sanitised edits to the DB.

    Returns ``(affected, created)`` where ``affected`` is the list of
    lesson ids changed by this call (new ids for creates, target ids for
    update/delete — in edit order, aligned with ``edits``) and ``created``
    the subset of ids that were newly inserted (for undo bookkeeping).
    Raises only on DB failure — call this inside the daemon's exception
    guard.
    """
    affected: list[int] = []
    created: list[int] = []
    for e in edits:
        if e.op == "create":
            lid = db.insert_lesson(
                session_id, e.content, scope=e.scope,
                rationale=e.rationale, origin=origin, round_id=round_id)
            affected.append(lid)
            created.append(lid)
        elif e.op == "update" and e.target_id is not None:
            db.update_lesson(e.target_id, e.content, e.rationale)
            affected.append(e.target_id)
        elif e.op == "delete" and e.target_id is not None:
            db.delete_lesson(e.target_id)
            affected.append(e.target_id)
    return affected, created


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

@dataclass
class _Snapshot:
    lesson_id: int
    existed: bool
    row: Optional[dict] = None


def undo_last_round(session_id: str) -> dict:
    """Revert the most recent refine round using its audit trail.

    Returns ``{"round_id", "reverted": [...], "error": None|str}``.
    Round rows store the pre-round state of every touched lesson and the
    ids created by the round (``detail`` = json object with ``snapshots``
    and ``created``), so undo needs only the DB.  Bare-list details from
    older rounds are also accepted.
    """
    events = db.query_refine_events(session_id, kind="refine", limit=5)
    rounds = [e for e in events if e.get("action") == "round"
              and e.get("round_id")]
    if not rounds:
        return {"round_id": 0, "reverted": [], "error": "no refine rounds"}
    target = rounds[0]
    round_id = int(target["round_id"])
    try:
        payload = json.loads(target.get("detail") or "[]")
    except json.JSONDecodeError:
        return {"round_id": round_id, "reverted": [],
                "error": "corrupt round audit detail"}
    # New format: {"snapshots": [...], "created": [ids]}; old: bare list.
    if isinstance(payload, dict):
        snapshots = payload.get("snapshots", [])
        created_ids = [int(x) for x in payload.get("created", [])]
    else:
        snapshots, created_ids = payload, []
    reverted = []
    for snap in snapshots:
        lid = int(snap["lesson_id"])
        try:
            if snap["existed"]:
                db.update_lesson(lid, snap["row"]["content"],
                                 snap["row"].get("rationale", ""))
                reverted.append(f"restored #{lid}")
            else:
                db.delete_lesson(lid)
                reverted.append(f"removed #{lid}")
        except Exception as exc:
            _log.warning("undo of lesson %s failed: %s", lid, exc)
            reverted.append(f"failed on #{lid}: {exc}")
    for lid in created_ids:
        try:
            db.delete_lesson(lid)
            reverted.append(f"deleted created #{lid}")
        except Exception as exc:
            _log.warning("undo delete of created lesson %s failed: %s",
                         lid, exc)
            reverted.append(f"failed on #{lid}: {exc}")
    db.insert_refine_event(session_id, "refine",
                           {"round_id": round_id}, action="undo",
                           detail=json.dumps(reverted), round_id=round_id)
    return {"round_id": round_id, "reverted": reverted, "error": None}


# ---------------------------------------------------------------------------
# Orchestration — the single entry point the daemon will call
# ---------------------------------------------------------------------------

def run_refine_round(
    session_id: str,
    task_summary: str,
    trajectory: str,
    llm_call: Callable[[str], str],
    *,
    core_memory: Optional[dict] = None,
    lessons: Optional[list[dict]] = None,
) -> RefineResult:
    """Run one full refine round: prompt -> LLM -> parse -> sanitise ->
    audit snapshot -> apply -> audit result.

    ``llm_call`` takes the prompt string and returns the raw response;
    any exception it raises is converted to a no-op round result so the
    conversation path is never disturbed.
    """
    core_memory = core_memory if core_memory is not None else {}
    lessons = db.load_lessons(session_id) if lessons is None else lessons
    history = db.query_refine_events(session_id, limit=REFINE_HISTORY_SHOWN)
    prompt = build_review_prompt(
        session_id=session_id, task_summary=task_summary,
        trajectory=trajectory, core_memory=core_memory,
        lessons=lessons, history=history)

    try:
        raw = llm_call(prompt)
    except Exception as exc:
        _log.warning("refine review failed (call): %s: %s",
                     type(exc).__name__, exc)
        return RefineResult(should_refine=False,
                            skipped=[f"review call failed: {exc}"])
    try:
        plan = parse_refine_response(raw)
    except Exception as exc:
        _log.warning("refine review failed (parse): %s: %s",
                     type(exc).__name__, exc)
        return RefineResult(should_refine=False,
                            skipped=[f"parse failed: {exc}"], raw=raw)

    result = RefineResult(should_refine=plan["should_refine"],
                          scope=plan["scope"], raw=plan)
    if not plan["should_refine"] or not plan["edits"]:
        return result

    edits, skipped = sanitize_edits(plan["edits"], lessons,
                                    default_scope=plan["scope"])
    result.skipped = skipped
    if not edits:
        return result

    round_id = db.last_refine_round(session_id) + 1
    # Snapshot the pre-round state of every existing lesson touched for
    # undo (creates have no pre-state; the id is recorded in ``created``).
    by_id = {r["id"]: r for r in db.load_lessons(
        session_id, include_global=True, limit=1000)}
    snapshots: list[_Snapshot] = []
    for lid in sorted({e.target_id for e in edits if e.target_id}):
        row = by_id.get(lid)
        snapshots.append(_Snapshot(lid, row is not None, row))

    affected, created = apply_edits(session_id, edits, round_id=round_id)
    result.applied = edits
    result.round_id = round_id

    db.insert_refine_event(
        session_id, "refine",
        {"reasoning": plan["reasoning"], "scope": plan["scope"],
         "edits": len(edits)},
        action="round",
        detail=json.dumps({
            "snapshots": [
                {"lesson_id": s.lesson_id, "existed": s.existed, "row": s.row}
                for s in snapshots],
            "created": created}),
        round_id=round_id)
    for e in edits:
        db.insert_refine_event(
            session_id, "refine",
            {"op": e.op, "id": e.target_id, "content": e.content[:200],
             "scope": e.scope},
            action=f"edit:{e.op}",
            detail=e.rationale, round_id=round_id)
    _log.info("refine round %d for %s: %d edits applied, %d skipped",
              round_id, session_id, len(edits), len(skipped))
    return result
