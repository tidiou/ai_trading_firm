"""
How old is the portfolio data an agent is reasoning from?

THE GAP. From 2 to 10 Nov 2026 every NVDA monitoring row cited the same
loss, -10.84% — the figure from the 30 Oct close, the last time the books
were closed. On 6 Nov Vera concluded that the whole move "was already in
place as of 30 Oct" and so there was nothing new; Solomon then repeated
the frozen figure as a fresh trigger on four different days. By 10 Nov
the real loss was 26%. Neither agent was told how old its numbers were,
so a stale figure read as a stable one.

Sections 1-2 are pure. Sections 3-4 read the agents and the ledger and
need the project's packages.
"""

from datetime import date

import pytest

from core.freshness import STALE_AFTER_SESSIONS, freshness_stamp


# ===============================================================
# 1. the stamp
# ===============================================================
class TestTheStamp:

    def test_one_session_old_is_normal(self):
        """The book is ALWAYS one close behind when the morning meeting
        runs. Flagging that would teach everyone to ignore the flag."""
        s = freshness_stamp(date(2026, 11, 11), 1)
        assert s["stale"] is False and s["sessions_old"] == 1
        assert "STALE" not in s["line"] and "Normal" in s["line"]

    def test_two_sessions_old_is_stale(self):
        """THE NVDA CASE: books last closed 30 Oct, read on 2 Nov."""
        s = freshness_stamp(date(2026, 10, 30), 2)
        assert s["stale"] is True
        assert "STALE" in s["line"] and "2026-10-30" in s["line"] and "2 sessions" in s["line"]

    def test_the_stale_line_says_what_to_distrust(self):
        line = freshness_stamp(date(2026, 10, 30), 9)["line"]
        assert "P&L" in line and "not at the most recent one" in line

    def test_a_current_ledger_is_just_current(self):
        s = freshness_stamp(date(2026, 11, 12), 0)
        assert s["stale"] is False and "current" in s["line"]

    def test_an_empty_ledger_is_not_stale_it_is_empty(self):
        s = freshness_stamp(None, 0, bootstrap=True)
        assert s["stale"] is False and "never been written" in s["line"]

    def test_an_unreadable_age_is_not_reported_as_fresh(self):
        """None, not False: 'could not tell' must not collapse into
        'fine'. A failed read that read as fresh would be the same
        silent failure this module exists to remove."""
        s = freshness_stamp(None, None, unreadable=True)
        assert s["stale"] is None and "unverified" in s["line"]

    def test_the_threshold_is_the_one_ada_refuses_at(self):
        assert freshness_stamp(date(2026, 1, 2), STALE_AFTER_SESSIONS)["stale"] is False
        assert freshness_stamp(date(2026, 1, 2), STALE_AFTER_SESSIONS + 1)["stale"] is True


# ===============================================================
# 2. the shape other code relies on
# ===============================================================
class TestTheShape:

    @pytest.mark.parametrize("args,kwargs", [
        ((date(2026, 11, 10), 2), {}),
        ((date(2026, 11, 11), 1), {}),
        ((None, 0), {"bootstrap": True}),
        ((None, None), {"unreadable": True}),
    ])
    def test_every_reading_carries_the_same_four_keys(self, args, kwargs):
        assert set(freshness_stamp(*args, **kwargs)) == \
            {"as_of", "sessions_old", "stale", "line"}

    def test_the_stamp_is_json_safe(self):
        """It rides in agent_runs.raw_output."""
        import json
        json.dumps(freshness_stamp(date(2026, 11, 10), 2))


# ===============================================================
# 3. what the agents are told
# ===============================================================
class TestTheAgentsAreTold:

    def test_vera_is_told_what_stale_means(self):
        from agents.vera import MONITORING_SYSTEM_PROMPT as P
        assert "DATA FRESHNESS" in P and "STALE" in P
        assert "NOT evidence that nothing has changed" in P

    def test_vera_is_shown_the_line(self):
        import inspect
        import agents.vera as vera
        src = inspect.getsource(vera.run)
        assert "Portfolio data freshness:" in src

    def test_vera_carries_it_in_her_output(self):
        import inspect
        import agents.vera as vera
        assert '"ledger_freshness"' in inspect.getsource(vera.run)

    def test_an_unreadable_age_does_not_cost_the_monitoring_pass(self):
        """The read is contained and degrades to 'unreadable'."""
        import inspect
        import agents.vera as vera
        src = inspect.getsource(vera.run)
        assert "unreadable=True" in src

    def test_solomon_is_told_a_repeated_frozen_figure_is_one_observation(self):
        from agents.solomon import SOLOMON_SYSTEM_PROMPT as P
        assert "LEDGER FRESHNESS" in P and "ONE observation" in P
        assert "not a" in P and "new trigger" in P

    def test_solomon_is_told_ada_will_refuse_on_a_stale_ledger(self):
        from agents.solomon import SOLOMON_SYSTEM_PROMPT as P
        assert "Ada also refuses" in P

    def test_solomon_is_shown_the_line_and_records_it(self):
        import inspect
        import agents.solomon as solomon
        src = inspect.getsource(solomon.run)
        assert "LEDGER FRESHNESS (how old" in src
        assert '"ledger_freshness": freshness' in src

    def test_solomon_copes_with_a_vera_that_reported_nothing(self):
        """dev_helpers' cached Vera output has no such key."""
        import inspect
        import agents.solomon as solomon
        assert "not reported by this run of Vera" in inspect.getsource(solomon.run)


# ===============================================================
# 4. one definition of "stale"
# ===============================================================
class TestOneDefinition:

    def test_the_ledger_takes_its_tolerance_from_the_freshness_module(self):
        """Two definitions would drift: Ada refusing at one threshold
        while the agents are told to distrust at another."""
        from core.ledger import MAX_LEDGER_STALENESS_SESSIONS
        assert MAX_LEDGER_STALENESS_SESSIONS == STALE_AFTER_SESSIONS

    def test_a_snapshot_reports_its_own_age(self):
        from core.ledger import LedgerSnapshot
        stale = LedgerSnapshot(nav=100.0, as_of=date(2026, 10, 30), staleness_sessions=9)
        assert stale.freshness()["stale"] is True
        fresh = LedgerSnapshot(nav=100.0, as_of=date(2026, 11, 11), staleness_sessions=1)
        assert fresh.freshness()["stale"] is False

    def test_what_ada_refuses_is_exactly_what_the_agents_distrust(self):
        from core.ledger import LedgerSnapshot
        for n in (0, 1, 2, 5):
            snap = LedgerSnapshot(nav=100.0, as_of=date(2026, 11, 1), staleness_sessions=n)
            assert snap.usable_for_sizing == (not snap.freshness()["stale"])

    def test_an_empty_ledger_reports_as_empty(self):
        from core.ledger import LedgerSnapshot
        snap = LedgerSnapshot(nav=None, as_of=None, staleness_sessions=0, bootstrap=True)
        assert "never been written" in snap.freshness()["line"]
