"""
D10 — the cycle is three scheduled runs, and the sweep no longer
cancels orders before the bell they were queued for.

THE DEFECT, IN ONE SENTENCE. The cycle ran at 16:30 ET; Ada submitted
DAY limit orders that the broker queued for the next session's open;
Otis's sweep ran minutes later in the same process, saw a closed market,
and cancelled them — recording `expired_unfilled`, which reads as the
market not reaching the limit rather than as the desk cancelling itself.

It had never fired because no cycle had yet placed an order. That is the
worst kind of defect to leave in: dormant, certain, and disguised as an
ordinary outcome the first time it happens.

Three things are tested here, all without a database, a broker or a
model:

  1. THE SESSION ARITHMETIC — has this order had its chance to fill.
  2. THE TIMETABLE — which group is due, when a group is overdue.
  3. THE SPLIT — the execution run refuses to act on a morning meeting
     that never happened, and Phase 3/4 stay skipped when Solomon saw
     no reason to act.

(This file used to contain two `pass` statements with TODOs. They had
also never run: an `orchestrator/` package directory shadowed
`orchestrator.py`, so `import orchestrator` resolved to the package and
the module under test was never imported at all.)
"""

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ===============================================================
# 1. "has this order had its chance to fill?"
# ===============================================================
class TestOrderSessionArithmetic:
    """
    The sweep used to ask `market_is_open_now()`. That is the right
    question for an order placed DURING a session and the wrong one for
    an order placed outside it — which, on the old 16:30 schedule, was
    every order the desk would ever place.

    The right question is: has the session this order was queued for
    closed? An order is live in the first session whose CLOSE falls at
    or after the moment it was submitted.
    """

    def test_an_order_placed_mid_session_belongs_to_that_session(self):
        from core.market_calendar import order_session_close
        close = order_session_close(datetime(2026, 9, 8, 9, 45, tzinfo=ET))
        assert close.astimezone(ET).date() == date(2026, 9, 8)
        assert close.astimezone(ET).hour == 16

    def test_a_premarket_order_belongs_to_that_morning(self):
        """Queued for the open a couple of hours later — not the next
        day. Getting this wrong would leave an order live for 24 hours."""
        from core.market_calendar import order_session_close
        close = order_session_close(datetime(2026, 9, 8, 8, 0, tzinfo=ET))
        assert close.astimezone(ET).date() == date(2026, 9, 8)

    def test_an_order_placed_after_the_close_belongs_to_the_next_session(self):
        """
        THE CASE THAT PRODUCED D10. Submitted 16:30 Friday 4 Sept; the
        next session is Tuesday 8 Sept, because Saturday and Sunday are
        weekends and Monday 7 Sept is Labor Day. The old guard swept it
        at 16:31 on the Friday.
        """
        from core.market_calendar import order_session_close
        close = order_session_close(datetime(2026, 9, 4, 16, 30, tzinfo=ET))
        assert close.astimezone(ET).date() == date(2026, 9, 8)

    def test_a_weekend_order_waits_for_the_next_session(self):
        from core.market_calendar import order_session_close
        close = order_session_close(datetime(2026, 9, 5, 12, 0, tzinfo=ET))
        assert close.astimezone(ET).date() == date(2026, 9, 8)

    def test_an_early_close_is_honoured(self):
        """
        Christmas Eve closes at 13:00 ET. An order submitted at 13:30
        that day is queued for the NEXT session — a naive 16:00
        assumption would sweep it that evening having given it no
        session at all. The calendar library already knows this; the
        arithmetic just has to ask it rather than assume.
        """
        from core.market_calendar import order_session_close
        close = order_session_close(datetime(2026, 12, 24, 13, 30, tzinfo=ET))
        assert close.astimezone(ET).date() > date(2026, 12, 24)

    def test_the_session_is_not_over_while_it_is_running(self):
        from core.market_calendar import order_session_is_over
        submitted = datetime(2026, 9, 8, 9, 45, tzinfo=ET)
        assert not order_session_is_over(submitted, now=datetime(2026, 9, 8, 12, 0, tzinfo=ET))

    def test_the_session_is_over_after_the_close(self):
        from core.market_calendar import order_session_is_over
        submitted = datetime(2026, 9, 8, 9, 45, tzinfo=ET)
        assert order_session_is_over(submitted, now=datetime(2026, 9, 8, 16, 30, tzinfo=ET))

    def test_the_four_sept_order_is_not_sweepable_that_evening(self):
        """The regression, stated as the scenario rather than as an
        abstraction: 16:31 on the Friday, the order must be left alone."""
        from core.market_calendar import order_session_is_over
        assert not order_session_is_over(
            datetime(2026, 9, 4, 16, 30, tzinfo=ET),
            now=datetime(2026, 9, 4, 16, 31, tzinfo=ET))

    def test_a_naive_timestamp_is_not_rejected(self):
        """psycopg can hand back a naive datetime depending on the
        column type and connection settings. Raising here would take
        down a reconciliation over a timezone detail."""
        from core.market_calendar import order_session_close
        assert order_session_close(datetime(2026, 9, 8, 13, 45)) is not None


# ===============================================================
# 2. Otis's per-order guard
# ===============================================================
class _FakeOrder:
    def __init__(self, recorded_at=None, status="accepted", broker_id="broker-1"):
        self.recorded_at = recorded_at
        self.status = status
        self.alpaca_order_id = broker_id
        self.ticker = "NVDA"


class TestSweepGuard:

    def test_an_order_whose_session_has_not_closed_is_left_alone(self, monkeypatch):
        from agents import otis
        monkeypatch.setattr(otis, "order_session_is_over", lambda t: False)
        may, why = otis._order_may_be_swept(
            _FakeOrder(recorded_at=datetime(2026, 9, 4, 16, 30, tzinfo=ET)))
        assert may is False
        assert "session closes" in why

    def test_an_order_whose_session_has_closed_is_swept(self, monkeypatch):
        from agents import otis
        monkeypatch.setattr(otis, "order_session_is_over", lambda t: True)
        may, _ = otis._order_may_be_swept(
            _FakeOrder(recorded_at=datetime(2026, 9, 8, 9, 45, tzinfo=ET)))
        assert may is True

    def test_a_legacy_row_keeps_the_old_behaviour_while_open(self, monkeypatch):
        """
        Rows written before migration 008 have no timestamp and cannot
        answer the question. They fall back to the rule the desk used
        for its whole life rather than being guessed at — inventing a
        placement time would be inventing the fact the sweep then acts
        on.
        """
        from agents import otis
        monkeypatch.setattr(otis, "market_is_open_now", lambda: True)
        may, why = otis._order_may_be_swept(_FakeOrder(recorded_at=None))
        assert may is False
        assert "migration 008" in why

    def test_a_legacy_row_is_swept_once_the_market_is_shut(self, monkeypatch):
        from agents import otis
        monkeypatch.setattr(otis, "market_is_open_now", lambda: False)
        may, _ = otis._order_may_be_swept(_FakeOrder(recorded_at=None))
        assert may is True

    def test_a_live_order_is_not_a_discrepancy(self, monkeypatch):
        """
        Since the guard leaves an order alone until its session closes,
        an order placed outside session hours is legitimately unresolved
        at reconciliation time. Flagging it would teach the reader that
        the discrepancy list contains routine states, which is how a
        control stops being read.
        """
        from agents import otis
        monkeypatch.setattr(otis, "order_session_is_over", lambda t: False)
        assert otis._order_is_still_live(
            _FakeOrder(recorded_at=datetime(2026, 9, 4, 16, 30, tzinfo=ET)))

    def test_a_terminal_order_is_never_live(self, monkeypatch):
        from agents import otis
        monkeypatch.setattr(otis, "order_session_is_over", lambda t: False)
        assert not otis._order_is_still_live(
            _FakeOrder(recorded_at=datetime(2026, 9, 4, 16, 30, tzinfo=ET),
                       status="filled"))

    def test_an_order_that_never_reached_the_broker_is_never_live(self, monkeypatch):
        from agents import otis
        monkeypatch.setattr(otis, "order_session_is_over", lambda t: False)
        assert not otis._order_is_still_live(
            _FakeOrder(recorded_at=datetime(2026, 9, 4, 16, 30, tzinfo=ET),
                       status="rejected_stale_ledger", broker_id=None))


# ===============================================================
# 3. the timetable
# ===============================================================
class TestTimetable:

    @pytest.mark.parametrize("hh,mm,expected", [
        (6, 0, None),          # too early for anything
        (7, 0, "premarket"),   # window opens
        (8, 30, "premarket"),  # the intended hour
        (9, 25, "premarket"),  # last minute before the bell
        (9, 26, None),         # deliberately dead: never hold the morning
        (9, 30, None),         #   meeting on the wrong side of the open
        (9, 45, "execution"),
        (12, 0, "execution"),
        (15, 30, "execution"),  # last moment leaving 30 min of session
        (15, 31, None),         # too late to place an order that can fill
        (16, 14, None),         # let the broker settle the last fills
        (16, 15, "close"),
        (16, 30, "close"),
        (22, 0, "close"),
        (23, 0, None),
    ])
    def test_which_group_is_due(self, hh, mm, expected):
        from core.cycle_schedule import due_group
        assert due_group(datetime(2026, 9, 8, hh, mm, tzinfo=ET)) == expected

    def test_the_windows_do_not_overlap(self):
        """`due_group` returns at most one group, which is only true if
        the windows are disjoint. Asserted directly so that editing one
        boundary cannot quietly create an overlap."""
        from core.cycle_schedule import GROUP_ORDER, PHASE_GROUPS
        spans = [(PHASE_GROUPS[k].opens, PHASE_GROUPS[k].closes) for k in GROUP_ORDER]
        for (_, a_close), (b_open, _) in zip(spans, spans[1:]):
            assert a_close < b_open, "phase group windows overlap"

    def test_execution_always_leaves_a_session_to_fill_in(self):
        """The point of the whole split. If this window could extend to
        the close, the defect comes straight back."""
        from core.cycle_schedule import PHASE_GROUPS
        assert PHASE_GROUPS["execution"].closes <= time(15, 30)

    def test_the_morning_meeting_ends_before_the_bell(self):
        from core.cycle_schedule import PHASE_GROUPS
        assert PHASE_GROUPS["premarket"].closes < time(9, 30)

    def test_cycle_rows_are_distinguishable_from_agents(self):
        """They share agent_runs with the eight agents. The dashboard
        counts agents; without this the cycle rows would inflate
        '6 of 9 completed' into '6 of 12'."""
        from core.cycle_schedule import cycle_row_name, is_cycle_row
        assert is_cycle_row(cycle_row_name("premarket"))
        for agent in ("atlas", "vera", "solomon", "nora_review", "nora_monitor",
                      "marcus", "ada", "otis", "clara", "clara_report"):
            assert not is_cycle_row(agent)


# ===============================================================
# 4. the split itself
# ===============================================================
@contextmanager
def _fake_track(_d, _name, _p):
    yield type("R", (), {"output": None})()


@pytest.fixture
def orch(monkeypatch):
    import orchestrator
    monkeypatch.setattr(orchestrator, "is_trading_day", lambda d: True)
    monkeypatch.setattr(orchestrator, "get_trading_control_state",
                        lambda: type("S", (), {"trading_enabled": True,
                                               "describe": lambda self: "ENABLED"})())
    monkeypatch.setattr(orchestrator, "track_agent_run", _fake_track)
    monkeypatch.setattr(orchestrator, "log_agent_run", lambda *a, **k: None)
    return orchestrator


class TestTheSplit:

    def test_execution_refuses_without_a_morning_meeting(self, orch, monkeypatch):
        """
        NOT THE SAME AS "no action needed", and the difference is the
        whole reason for the guard. A missing verdict means the morning
        meeting did not happen or did not finish. Treating that as
        "nothing to do" would make a failed premarket run silently
        indistinguishable from a quiet one — and treating it as
        "proceed" would trade on nothing at all.
        """
        called = []
        monkeypatch.setattr(orch, "load_agent_output",
                            lambda d, n, require_completed=True: None)
        monkeypatch.setattr(orch.nora, "review_proposals",
                            lambda d: called.append("nora"))
        monkeypatch.setattr(orch.marcus, "run", lambda d: called.append("marcus"))
        monkeypatch.setattr(orch.ada, "run", lambda d: called.append("ada"))

        result = orch.run_execution(date(2026, 9, 8))

        assert called == [], "nothing may run without a verdict"
        assert result["skipped"] == "no morning meeting on record"
        assert "execution_gate" in result["errors"], "and it must be recorded as an error"

    def test_a_failed_solomon_run_is_not_a_verdict(self, monkeypatch):
        """`load_agent_output` returns None for a row that did not
        complete — a half-written verdict is not one."""
        import core.logging_utils as lu

        class _Row:
            status = "failed"
            raw_output = {"action_needed": True}

        class _Q:
            def query(self, _m): return self
            def filter(self, *_a): return self
            def first(self): return _Row()
            def __enter__(self): return self
            def __exit__(self, *_a): return False

        monkeypatch.setattr(lu, "session_scope", lambda: _Q())
        assert lu.load_agent_output(date(2026, 9, 8), "solomon") is None

    def test_a_truncated_verdict_is_not_a_verdict(self, monkeypatch):
        """Oversized outputs are stored as a text preview. Parsing a
        preview back into a trading decision would be a guess dressed
        up as a lookup."""
        import core.logging_utils as lu

        class _Row:
            status = "completed"
            raw_output = {"truncated": True, "preview": '{"action_needed": true'}

        class _Q:
            def query(self, _m): return self
            def filter(self, *_a): return self
            def first(self): return _Row()
            def __enter__(self): return self
            def __exit__(self, *_a): return False

        monkeypatch.setattr(lu, "session_scope", lambda: _Q())
        assert lu.load_agent_output(date(2026, 9, 8), "solomon") is None

    def test_phases_3_and_4_stay_skipped_when_no_action_is_needed(self, orch, monkeypatch):
        """The original TODO in this file, finally asserted. Most days
        end here, and that is the design rather than a failure."""
        called = []
        monkeypatch.setattr(orch, "load_agent_output",
                            lambda d, n, require_completed=True: {"action_needed": False})
        monkeypatch.setattr(orch.nora, "review_proposals",
                            lambda d: called.append("nora"))
        monkeypatch.setattr(orch.marcus, "run", lambda d: called.append("marcus"))
        monkeypatch.setattr(orch.ada, "run", lambda d: called.append("ada"))

        result = orch.run_execution(date(2026, 9, 8))

        assert called == []
        assert result["skipped"] == "solomon: no action needed"
        assert not result["errors"], "a quiet day is not an error"

    def test_execution_runs_the_chain_when_action_is_needed(self, orch, monkeypatch):
        called = []
        monkeypatch.setattr(orch, "load_agent_output",
                            lambda d, n, require_completed=True: {"action_needed": True})
        monkeypatch.setattr(orch.nora, "review_proposals", lambda d: (
            called.append("nora"),
            {"portfolio_status": "within_limits", "circuit_breaker_active": False})[1])
        monkeypatch.setattr(orch.marcus, "run", lambda d: (
            called.append("marcus"),
            {"allocations": [{"target_size_pct": 5.6}]})[1])
        monkeypatch.setattr(orch.ada, "run", lambda d: (
            called.append("ada"), {"orders": [{"ticker": "NVDA"}]})[1])

        result = orch.run_execution(date(2026, 9, 8))

        assert called == ["nora", "marcus", "ada"], "the run order is the control"
        assert len(result["ada"]["orders"]) == 1

    def test_ada_is_skipped_when_marcus_sizes_nothing(self, orch, monkeypatch):
        called = []
        monkeypatch.setattr(orch, "load_agent_output",
                            lambda d, n, require_completed=True: {"action_needed": True})
        monkeypatch.setattr(orch.nora, "review_proposals", lambda d: {
            "portfolio_status": "within_limits", "circuit_breaker_active": False})
        monkeypatch.setattr(orch.marcus, "run", lambda d: {
            "allocations": [{"target_size_pct": 0}]})
        monkeypatch.setattr(orch.ada, "run", lambda d: called.append("ada"))

        result = orch.run_execution(date(2026, 9, 8))

        assert called == []
        assert result["skipped"] == "marcus: no actionable allocations"

    def test_the_premarket_run_submits_nothing(self, orch):
        """Structural, not incidental: the group that talks to the
        market is a different process from the one that reads it."""
        import inspect
        src = inspect.getsource(orch.run_premarket)
        assert "ada" not in src
        assert "marcus" not in src


# ===============================================================
# 5. `auto` — what the scheduler actually calls
# ===============================================================
class TestAuto:

    def test_a_non_trading_day_does_nothing(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "is_trading_day", lambda d: False)
        out = orch.run_auto(datetime(2026, 9, 5, 9, 45, tzinfo=ET))
        assert out["action"] == "skipped" and out["reason"] == "not a trading day"

    @pytest.mark.parametrize("hh,mm", [(6, 0), (9, 30), (16, 0), (23, 30)])
    def test_outside_every_window_does_nothing(self, orch, hh, mm):
        """09:30 and 16:00 are inside the trading day and deliberately
        outside every window — the gaps are where the timetable says
        'not now', and `auto` must respect them rather than treating any
        session minute as fair game."""
        out = orch.run_auto(datetime(2026, 9, 8, hh, mm, tzinfo=ET))
        assert out["action"] == "skipped" and out["reason"] == "outside every window"

    def test_a_completed_group_is_not_run_twice(self, orch, monkeypatch):
        """
        THE PROPERTY THAT MAKES A 15-MINUTE TICK SAFE. Without it, `auto`
        would re-run the morning meeting twenty times before the bell and
        re-run Ada across the whole session.
        """
        monkeypatch.setattr(orch, "group_state", lambda d, g: "completed")
        out = orch.run_auto(datetime(2026, 9, 8, 9, 45, tzinfo=ET))
        assert out["action"] == "skipped" and out["reason"] == "already completed"

    def test_a_running_group_is_not_started_again(self, orch, monkeypatch):
        """The premarket group takes minutes of model calls. Two Adas
        racing for the same allocations would probably be caught by her
        idempotency guard, and 'probably' is not a control."""
        monkeypatch.setattr(orch, "group_state", lambda d, g: "running")
        out = orch.run_auto(datetime(2026, 9, 8, 9, 45, tzinfo=ET))
        assert out["action"] == "skipped" and out["reason"] == "already running"

    def test_a_failed_group_is_retried(self, orch, monkeypatch):
        """A dropped connection or a rate-limited API is exactly the
        case worth retrying on the next tick."""
        ran = []
        monkeypatch.setattr(orch, "group_state", lambda d, g: "failed")
        monkeypatch.setattr(orch, "_attempts_so_far", lambda d, g: 1)
        monkeypatch.setattr(orch, "run_group", lambda g, d: ran.append(g) or {"errors": {}})
        out = orch.run_auto(datetime(2026, 9, 8, 9, 45, tzinfo=ET))
        assert out["action"] == "ran" and ran == ["execution"]

    def test_retries_are_capped(self, orch, monkeypatch):
        """Past three it is not transient, and retrying every fifteen
        minutes until the window shuts spends real money failing the
        same way twenty times."""
        from core.cycle_schedule import MAX_ATTEMPTS_PER_GROUP
        ran = []
        monkeypatch.setattr(orch, "group_state", lambda d, g: "failed")
        monkeypatch.setattr(orch, "_attempts_so_far",
                            lambda d, g: MAX_ATTEMPTS_PER_GROUP)
        monkeypatch.setattr(orch, "run_group", lambda g, d: ran.append(g))
        out = orch.run_auto(datetime(2026, 9, 8, 9, 45, tzinfo=ET))
        assert out["action"] == "skipped" and out["reason"] == "attempt limit reached"
        assert ran == []


# ===============================================================
# 6. "did the desk run today?"
# ===============================================================
class TestOverdueDetection:
    """
    The only failure mode that leaves no trace anywhere else. A crashed
    agent writes a `failed` row; a laptop asleep at 09:45 writes nothing
    at all, and every table looks exactly like a quiet day. Absence is
    only detectable against an expectation.
    """

    def test_nothing_is_overdue_before_its_hour(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "group_state", lambda d, g: "not_run")
        state = orch.cycle_status(now=datetime(2026, 9, 8, 8, 0, tzinfo=ET))
        assert not state["any_overdue"]

    def test_a_missed_morning_is_overdue_by_the_bell(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "group_state", lambda d, g: "not_run")
        state = orch.cycle_status(now=datetime(2026, 9, 8, 10, 0, tzinfo=ET))
        overdue = {g["key"] for g in state["groups"] if g["overdue"]}
        assert overdue == {"premarket"}

    def test_a_group_stuck_running_counts_as_overdue(self, orch, monkeypatch):
        """A process that died mid-flight leaves a `running` row forever.
        Treating that as 'in progress' at 5pm would hide it."""
        monkeypatch.setattr(orch, "group_state",
                            lambda d, g: "running" if g == "close" else "completed")
        state = orch.cycle_status(now=datetime(2026, 9, 8, 19, 0, tzinfo=ET))
        assert {g["key"] for g in state["groups"] if g["overdue"]} == {"close"}

    def test_a_full_day_is_never_overdue(self, orch, monkeypatch):
        monkeypatch.setattr(orch, "group_state", lambda d, g: "completed")
        state = orch.cycle_status(now=datetime(2026, 9, 8, 23, 0, tzinfo=ET))
        assert not state["any_overdue"]

    def test_a_holiday_is_not_overdue(self, orch, monkeypatch):
        """Labor Day. Nothing was scheduled, so nothing is late — and an
        alert that cries wolf every public holiday is an alert nobody
        reads on the day it matters."""
        monkeypatch.setattr(orch, "is_trading_day", lambda d: False)
        monkeypatch.setattr(orch, "group_state", lambda d, g: "not_run")
        state = orch.cycle_status(now=datetime(2026, 9, 7, 19, 0, tzinfo=ET))
        assert not state["any_overdue"]
        assert all(g["state"] == "not_applicable" for g in state["groups"])


# ===============================================================
# 7. the retry counter survives the thing it counts
#
# Found while reviewing the split rather than by running it, and worth
# recording as its own section because the first implementation was
# quietly broken in a way that looked fine.
#
# `track_agent_run` writes raw_output=None on the failure path. That is
# right for an agent — its output does not exist if it raised — and
# fatal for a counter stored in that column: the attempt number would
# vanish on exactly the runs worth counting, `_attempts_so_far` would
# read zero forever, and the cap meant to stop a broken group retrying
# every fifteen minutes until its window shut would never engage.
#
# The second way it could break is subtler: the group row used to carry
# the whole nested result, and Vera's output alone can exceed the 64k
# truncation limit in core.logging_utils. A truncated row reads back as
# no row at all. Same broken counter, from a completely different cause.
# ===============================================================
class TestRetryCounter:

    def test_the_group_row_does_not_use_track_agent_run(self):
        """The structural guarantee behind the two tests below."""
        import ast
        import inspect
        import orchestrator

        tree = ast.parse(inspect.getsource(orchestrator.run_group).strip())
        fn = tree.body[0]
        # Drop the docstring — it explains at length why track_agent_run
        # is not used here, and a naive substring check would trip on
        # its own explanation.
        body = ast.unparse(ast.Module(body=fn.body[1:], type_ignores=[]))
        assert "track_agent_run" not in body
        assert "log_agent_run" in body

    def test_the_attempt_count_is_written_on_the_failure_path(self):
        import inspect
        import orchestrator
        src = inspect.getsource(orchestrator.run_group)
        failure = src[src.index('"failed"'):]
        assert '"attempt": attempt' in failure, \
            "a failed group must still record which attempt it was"

    def test_a_failed_row_is_readable_by_the_counter(self, monkeypatch):
        import core.logging_utils as lu

        class _Row:
            status = "failed"
            raw_output = {"group": "premarket", "attempt": 2}

        class _Q:
            def query(self, _m): return self
            def filter(self, *_a): return self
            def first(self): return _Row()
            def __enter__(self): return self
            def __exit__(self, *_a): return False

        monkeypatch.setattr(lu, "session_scope", lambda: _Q())
        assert lu.load_agent_output(date(2026, 9, 8), "cycle:premarket") is None, \
            "a failed row is not a result"
        readable = lu.load_agent_output(date(2026, 9, 8), "cycle:premarket",
                                        require_completed=False)
        assert readable["attempt"] == 2, "...but the counter must still see it"

    def test_the_counter_climbs_across_failures(self, orch, monkeypatch):
        """The behaviour the cap depends on, end to end."""
        seen = {"attempt": 0}
        monkeypatch.setattr(orch, "load_agent_output",
                            lambda d, n, require_completed=True: dict(seen))
        for expected in (1, 2, 3):
            seen["attempt"] = expected - 1
            assert orch._attempts_so_far(date(2026, 9, 8), "premarket") + 1 == expected

    def test_the_group_row_stays_small(self, orch):
        """A summary, not a second copy of every agent's output — the
        other way the counter could have been silently truncated away."""
        import json
        big = {"date": date(2026, 9, 8),
               "vera": {"candidates": ["x" * 40_000]},
               "atlas": {"regime_signal": "neutral"},
               "solomon": {"action_needed": True},
               "errors": {}}
        summary = orch._cycle_summary(big, "premarket", 2, True)
        assert len(json.dumps(summary, default=str)) < 2_000
        assert summary["attempt"] == 2
        assert set(summary["agents_completed"]) == {"atlas", "vera", "solomon"}

    def test_a_group_that_finished_with_errors_is_completed_not_failed(self, orch):
        """The close group swallows per-agent failures on purpose so Otis
        cannot stop Clara. That is a completed run carrying errors — but
        the errors must reach the row, or the day reads as clean."""
        summary = orch._cycle_summary(
            {"otis": None, "clara": {"process_check": "clean"},
             "errors": {"otis": "RuntimeError: Alpaca unreachable"}},
            "close", 1, True)
        assert "otis" in summary["errors"]
        assert summary["agents_completed"] == ["clara"]
