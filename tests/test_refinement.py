"""Tests for nbchat.core.refinement (prime-agent /refine port).

Covers:
- should_refine_task triggers
- build_review_prompt / parse_refine_response (fenced JSON, strict errors)
- sanitize_edits (caps, dedup-merge, bad ids, global-delete refusal)
- run_refine_round end-to-end with a scripted llm_call on a temp DB
- undo_last_round restoring pre-round state from the audit trail
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Point the DB at a throwaway file BEFORE importing the module under test.
_tmp = tempfile.mkdtemp(prefix="nbchat_refine_test_")
os.environ["NBCCHAT_TEST_DB"] = os.path.join(_tmp, "test.db")

from nbchat.core import db as dbmod  # noqa: E402

dbmod.DB_PATH = Path(_tmp) / "test.db"
dbmod.init_db()

from nbchat.core import refinement as rf  # noqa: E402


SESSION = "refine-test-sess"


# ── trigger ──────────────────────────────────────────────────────────────

def test_trigger_completed_task():
    ok, reason = rf.should_refine_task(status="done")
    assert ok and "completed" in reason


def test_trigger_failed_task():
    ok, reason = rf.should_refine_task(status="failed")
    assert ok and "failed" in reason


def test_trigger_many_tool_failures():
    ok, reason = rf.should_refine_task(status="in_progress", failed_tools=3)
    assert ok and "3" in reason


def test_no_trigger_quiet_task():
    ok, _ = rf.should_refine_task(status="in_progress", failed_tools=1)
    assert not ok


# ── prompt / parse ────────────────────────────────────────────────────────

def test_prompt_contains_state():
    p = rf.build_review_prompt(
        session_id="s1", task_summary="fix the parser",
        trajectory="tool: run_command ok",
        core_memory={"goal": "fix the parser"},
        lessons=[{"id": 7, "content": "always run tests", "scope": "global",
                  "useful": 2}],
        history=[{"round_id": 1, "action": "round", "detail": "x"}])
    assert "refine-test-sess" not in p  # session id is s1
    assert "s1" in p and "fix the parser" in p
    assert "[7]" in p  # lesson ids shown for addressing
    assert "STRICT JSON" in p


def test_parse_plain_json():
    raw = json.dumps({
        "should_refine": True, "scope": "session",
        "edits": [{"op": "create", "content": "X", "rationale": "y"}],
    })
    plan = rf.parse_refine_response(raw)
    assert plan["should_refine"] and plan["scope"] == "session"
    assert plan["edits"][0]["op"] == "create"


def test_parse_fenced_json():
    raw = "```json\n" + json.dumps({"edits": []}) + "\n```"
    assert rf.parse_refine_response(raw)["edits"] == []


def test_parse_rejects_bad_op():
    with pytest.raises(rf.RefineParseError):
        rf.parse_refine_response(
            json.dumps({"edits": [{"op": "overwrite", "content": "x"}]}))


def test_parse_rejects_garbage():
    with pytest.raises(rf.RefineParseError):
        rf.parse_refine_response("no json here at all")


# ── sanitize ──────────────────────────────────────────────────────────────

def _lesson(rid, content, scope="session", useful=0):
    return {"id": rid, "content": content, "scope": scope, "useful": useful}


def test_sanitize_cap():
    edits, skipped = rf.sanitize_edits(
        [{"op": "create", "content": f"lesson {i}"} for i in range(8)],
        [])
    assert len(edits) == rf.REFINE_MAX_EDITS
    assert any("cap" in s for s in skipped)


def test_sanitize_dedup_merges_into_update():
    existing = [_lesson(3, "always run the test suite before pushing")]
    edits, _ = rf.sanitize_edits(
        [{"op": "create", "content": "Always run the test suite before pushing"}],
        list(existing))
    assert len(edits) == 1
    assert edits[0].op == "update" and edits[0].target_id == 3
    assert "dedup" in edits[0].rationale


def test_sanitize_bad_id_and_unknown_id():
    edits, skipped = rf.sanitize_edits(
        [{"op": "delete", "id": "abc"},
         {"op": "delete", "id": 99}],
        [_lesson(1, "x")])
    assert edits == []
    assert len(skipped) == 2


def test_sanitize_refuses_global_delete():
    edits, skipped = rf.sanitize_edits(
        [{"op": "delete", "id": 1}], [_lesson(1, "x", scope="global")])
    assert edits == []
    assert any("global" in s for s in skipped)


def test_sanitize_truncates_long_content():
    edits, _ = rf.sanitize_edits(
        [{"op": "create", "content": "a" * 900}], [])
    assert len(edits[0].content) <= rf.REFINE_MAX_CONTENT_CHARS


# ── end-to-end round on the temp DB ───────────────────────────────────────

def test_round_creates_and_rolls_back():
    def scripted(prompt):
        assert "refine" in prompt.lower()
        return json.dumps({
            "should_refine": True, "scope": "session",
            "reasoning": "learned a workflow",
            "edits": [
                {"op": "create", "content": "Use repo_overview before grep",
                 "rationale": "saved 5 turns"},
                {"op": "delete", "id": 12345},  # unknown id -> skipped
            ],
        })

    before = dbmod.load_lessons(SESSION)
    result = rf.run_refine_round(
        SESSION, "fix the parser", "tool: run_command -> ok", scripted)
    assert len(result.applied) == 1 and result.round_id == 1
    assert any("unknown lesson id" in s for s in result.skipped)
    after = dbmod.load_lessons(SESSION)
    assert len(after) == len(before) + 1

    # Rollback restores the pre-round state.
    undo = rf.undo_last_round(SESSION)
    assert undo["error"] is None and undo["reverted"]
    final = dbmod.load_lessons(SESSION)
    assert len(final) == len(before)

    # Audit trail records the round and the undo.
    events = dbmod.query_refine_events(SESSION)
    actions = [e["action"] for e in events]
    assert "round" in actions and "edit:create" in actions and "undo" in actions


def test_round_updates_existing_lesson():
    lid = dbmod.insert_lesson(SESSION, "old wording", scope="session",
                              rationale="seed", origin="seed")
    def scripted(prompt):
        return json.dumps({
            "should_refine": True,
            "edits": [{"op": "update", "id": lid,
                       "content": "improved wording",
                       "rationale": "clearer"}],
        })
    result = rf.run_refine_round(
        SESSION, "task", "traj", scripted)
    assert len(result.applied) == 1
    row = dbmod.get_lesson(lid)
    assert row and row["content"] == "improved wording"
    undo = rf.undo_last_round(SESSION)
    assert undo["error"] is None
    assert dbmod.get_lesson(lid)["content"] == "old wording"


def test_round_llm_failure_is_noop():
    def broken(prompt):
        raise TimeoutError("server went quiet")
    result = rf.run_refine_round(SESSION, "task", "traj", broken)
    assert not result.applied and result.round_id == 0
    assert any("review call failed" in s for s in result.skipped)


def test_round_parse_failure_is_noop():
    result = rf.run_refine_round(
        SESSION, "task", "traj", lambda p: "I would suggest the following…")
    assert not result.applied
    assert any("parse failed" in s for s in result.skipped)


def test_round_declines_to_refine():
    result = rf.run_refine_round(
        SESSION, "task", "traj",
        lambda p: json.dumps({"should_refine": False, "edits": []}))
    assert result.should_refine is False and not result.applied
