"""Tests for the main-agent token meter (team_metrics) added for tui3 /budget.

The main-agent meter is a process-global token counter (separate from the
/team run meter) that backs the tui3 ``/budget`` command.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbchat.core import team_metrics as tm


def test_main_meter_accumulates():
    tm.reset_main_tokens()
    tm.record_tokens(100)
    tm.record_tokens(50)
    stats = tm.main_token_stats()
    assert stats["total_tokens"] == 150
    assert stats["calls"] == 2


def test_main_meter_reset():
    tm.reset_main_tokens()
    tm.record_tokens(999)
    stats = tm.main_token_stats()
    assert stats["total_tokens"] == 999
    tm.reset_main_tokens()
    stats = tm.main_token_stats()
    assert stats["total_tokens"] == 0
    assert stats["calls"] == 0


def test_record_tokens_noop_for_none():
    tm.reset_main_tokens()
    # A None/0 token count must not increment the call count.
    tm.record_tokens(0)
    tm.record_tokens(None)
    stats = tm.main_token_stats()
    assert stats["calls"] == 0
    assert stats["total_tokens"] == 0


def test_main_meter_and_team_meter_coexist():
    # When a team meter is active, record_tokens reports to BOTH meters.
    tm.reset_main_tokens()
    team_meter = tm._TokenMeter()
    tm._set_active_meter(team_meter)
    try:
        tm.record_tokens(200)
        main = tm.main_token_stats()
        assert main["total_tokens"] == 200
        with team_meter._lock:
            assert team_meter.total_tokens == 200
    finally:
        tm._set_active_meter(None)


def test_main_meter_when_no_team_active():
    # When no team meter is active, record_tokens still reports to the main
    # meter (the tui3 /budget view).
    tm.reset_main_tokens()
    tm._set_active_meter(None)
    tm.record_tokens(321)
    stats = tm.main_token_stats()
    assert stats["total_tokens"] == 321
    assert stats["calls"] == 1
