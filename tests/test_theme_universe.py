"""
The screening universe is the AI data-centre chain, screened per link.

WHAT THIS REPLACED, AND WHY IT MATTERED. The universe was six US
mega-caps — AAPL, MSFT, GOOGL, AMZN, NVDA, JPM — and that list was
never a strategy. It was picked to survive FMP's free-tier sample
restriction, which quietly made a data vendor's sample list the desk's
research mandate. By 2026-09-23 it had degenerated to a THREE-name
screen once holdings and an open thesis were excluded, and on that
day's run the best conviction in the whole universe was a 3.

Two things had to move together, and the tests below pin both:

  THE UNIVERSE, from six names to the 27 in core/theme.py. That was
  only possible once the data path stopped going through an endpoint
  that 402s outside a fixed list — see TestTheDataPath.

  THE SHAPE, from one pass to seven. Each link of the chain turns on
  something different: a power name lives or dies on backlog
  converting, a compute name on share as workloads shift to inference.
  One undifferentiated pass asks one question of seven businesses.

The link ORDER is the house view written down — power first because
power is the binding constraint, hyperscalers last because they are
the payers. A test that pins the order is a test that pins the
strategy, which is why it is asserted here rather than left to a
comment.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from agents import vera
from core.theme import THEME_LINKS, THEME_OF, THEME_UNIVERSE

TODAY = date(2026, 9, 23)


# =================================================================
# 1. The plan: which links, which names, in which order
# =================================================================
class TestLinkBatches:

    def test_every_link_is_screened(self):
        batches = vera.link_batches()
        assert [link for link, _ in batches] == vera.SCREENING_ORDER
        assert len(batches) == len(THEME_LINKS)

    def test_the_order_is_bottleneck_first(self):
        # THE STRATEGY, PINNED. Power is the binding constraint today;
        # the hyperscalers are the payers whose capex is everyone
        # else's revenue, so they are read last. Changing this is a
        # change of house view and should have to change a test.
        assert vera.SCREENING_ORDER == [
            "power", "cooling_infra", "foundry_equipment", "memory",
            "networking", "compute", "hyperscaler",
        ]

    def test_power_comes_before_compute(self):
        order = [link for link, _ in vera.link_batches()]
        assert order.index("power") < order.index("compute")

    def test_hyperscalers_are_read_last(self):
        assert vera.link_batches()[-1][0] == "hyperscaler"

    def test_every_theme_name_appears_exactly_once(self):
        seen = [t for _, tickers in vera.link_batches() for t in tickers]
        assert sorted(seen) == THEME_UNIVERSE
        assert len(seen) == len(set(seen)), "a name screened twice"

    def test_excluded_names_are_removed_from_their_link(self):
        batches = dict(vera.link_batches(excluded={"NVDA", "GEV"}))
        assert "NVDA" not in batches["compute"]
        assert "GEV" not in batches["power"]
        assert "AMD" in batches["compute"]

    def test_a_fully_held_link_is_dropped_not_sent_empty(self):
        # memory is a single accessible name. Holding it must not send
        # an empty ticker list to the model.
        batches = dict(vera.link_batches(excluded={"MU"}))
        assert "memory" not in batches

    def test_holding_everything_yields_no_batches(self):
        assert vera.link_batches(excluded=set(THEME_UNIVERSE)) == []

    def test_a_fully_invested_book_is_not_reported_as_a_failed_pass(self):
        # "Nothing came back" and "there was nothing left to ask about"
        # are different facts. Conflating them sends someone looking
        # for an outage that isn't one.
        line = vera.describe_screening({
            "universe_size": 0, "screened_count": 0, "surfaced_count": 0,
            "excluded_as_held": sorted(THEME_UNIVERSE), "not_returned": [],
            "surfaced_tickers": [], "best_conviction": None,
            "best_conviction_ticker": None, "threshold": 4,
        })
        assert "Nothing left to screen" in line
        assert "failed pass" not in line

    def test_no_batch_is_larger_than_five_names(self):
        # The token budget per link assumes this. If theme.py grows a
        # link past it, this is where that shows up.
        for link, tickers in vera.link_batches():
            assert len(tickers) <= 5, (link, tickers)

    def test_it_accepts_a_list_or_a_set(self):
        assert (vera.link_batches(["NVDA"]) == vera.link_batches({"NVDA"}))

    def test_a_name_in_two_links_is_screened_under_the_first(self, monkeypatch):
        # theme.py's own comment flags AVGO as arguably both compute
        # and networking, so this is one edit away from mattering.
        # Screening it twice would also over-count universe_size
        # against a THEME_UNIVERSE that de-duplicates.
        links = {k: dict(v) for k, v in THEME_LINKS.items()}
        links["networking"] = dict(links["networking"],
                                   tickers=links["networking"]["tickers"] + ["AVGO"])
        monkeypatch.setattr(vera, "THEME_LINKS", links)
        batches = dict(vera.link_batches())
        assert "AVGO" in batches["networking"]      # networking is read first
        assert "AVGO" not in batches["compute"]
        names = [t for _, ts in vera.link_batches() for t in ts]
        assert len(names) == len(set(names))


class TestTheOrderCannotSilentlyDrift:

    def test_screening_order_covers_theme_links_exactly(self):
        # The state the desk should be in. When it isn't, the two tests
        # below say what happens instead.
        assert set(vera.SCREENING_ORDER) == set(THEME_LINKS)

    def test_a_link_missing_from_the_order_is_still_screened(self, monkeypatch):
        # A coverage gap in one link is bad; a failed import of
        # agents/vera.py is the whole desk down. The unknown link goes
        # last rather than going missing.
        monkeypatch.setattr(vera, "SCREENING_ORDER",
                            [l for l in vera.SCREENING_ORDER if l != "memory"])
        order = vera.screening_order()
        assert "memory" in order
        assert order[-1] == "memory"
        assert set(order) == set(THEME_LINKS)

    def test_it_warns_about_the_omission(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(vera, "SCREENING_ORDER",
                            [l for l in vera.SCREENING_ORDER if l != "power"])
        with caplog.at_level(logging.WARNING, logger=vera.logger.name):
            vera.screening_order()
        assert any("power" in r.getMessage() for r in caplog.records)

    def test_an_order_entry_theme_py_dropped_is_ignored(self, monkeypatch):
        monkeypatch.setattr(vera, "SCREENING_ORDER",
                            vera.SCREENING_ORDER + ["retired_link"])
        assert "retired_link" not in vera.screening_order()

    def test_the_universe_is_the_theme_universe(self):
        assert vera.FIXED_UNIVERSE == THEME_UNIVERSE
        assert len(vera.FIXED_UNIVERSE) == 27

    def test_the_old_six_name_universe_is_gone(self):
        # Two of the six were never part of this chain.
        assert "JPM" not in vera.FIXED_UNIVERSE
        assert "AAPL" not in vera.FIXED_UNIVERSE


# =================================================================
# 2. The prompt each link gets
# =================================================================
class TestDescribeLink:

    def test_it_carries_the_dependency_not_just_the_name(self):
        text = vera.describe_link("power")
        assert THEME_LINKS["power"]["depends_on"] in text
        assert THEME_LINKS["power"]["note"] in text

    def test_each_link_asks_a_different_question(self):
        described = {link: vera.describe_link(link) for link in THEME_LINKS}
        assert len(set(described.values())) == len(THEME_LINKS)

    def test_it_comes_from_theme_py_not_a_local_copy(self):
        # One definition of the chain, used by the screen, by Atlas's
        # read-across and by risk aggregation. A second copy here would
        # drift.
        for link, spec in THEME_LINKS.items():
            assert spec["label"] in vera.describe_link(link)


class TestScreenLinkPrompt:

    def test_the_prompt_names_only_this_links_tickers(self, monkeypatch):
        seen = {}

        def fake_loop(system_prompt, user_prompt, **kw):
            seen["prompt"] = user_prompt
            seen["max_tokens"] = kw.get("max_tokens")
            return "[]"

        monkeypatch.setattr(vera, "run_agent_loop", fake_loop)
        vera.screen_link("memory", ["MU"], TODAY, {"regime_signal": "risk_on"})
        assert "MU" in seen["prompt"]
        assert "NVDA" not in seen["prompt"]
        assert THEME_LINKS["memory"]["depends_on"] in seen["prompt"]

    def test_the_token_budget_is_per_link_not_per_universe(self, monkeypatch):
        seen = {}

        def fake_loop(system_prompt, user_prompt, **kw):
            seen["max_tokens"] = kw.get("max_tokens")
            return "[]"

        monkeypatch.setattr(vera, "run_agent_loop", fake_loop)
        vera.screen_link("power", THEME_LINKS["power"]["tickers"], TODAY, {})
        # Five names against the budget the old pass gave twenty-seven.
        assert seen["max_tokens"] >= 4000

    def test_a_failure_propagates_so_the_caller_can_contain_it(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(vera, "run_agent_loop", boom)
        with pytest.raises(RuntimeError):
            vera.screen_link("memory", ["MU"], TODAY, {})


# =================================================================
# 3. A failed link costs one link
# =================================================================
class TestOneFailedLinkIsNotALostDay:
    """The whole justification for seven passes instead of one. If a
    failure took the run down anyway, the extra round trips would be
    pure cost."""

    @pytest.fixture
    def run_with_failing_link(self, monkeypatch):
        def go(failing: str):
            from contextlib import contextmanager

            from tests.test_vera import FakeSession

            @contextmanager
            def scope():
                yield FakeSession([])

            calls = []

            def fake_screen(link, tickers, today, atlas):
                calls.append(link)
                if link == failing:
                    raise RuntimeError("link exploded")
                return []

            monkeypatch.setattr(vera, "session_scope", scope)
            monkeypatch.setattr(vera, "get_open_theses", lambda s: [])
            monkeypatch.setattr(vera, "get_position_tickers", lambda s: [])
            monkeypatch.setattr(vera, "review_orphan_positions", lambda d: [])
            monkeypatch.setattr(vera, "screen_link", fake_screen)
            return vera.run(TODAY, {"regime_signal": "risk_on"}), calls
        return go

    def test_the_other_six_links_still_run(self, run_with_failing_link):
        _, calls = run_with_failing_link("power")
        assert calls == vera.SCREENING_ORDER
        assert len(calls) == 7

    def test_a_failure_in_the_FIRST_link_does_not_stop_the_rest(
            self, run_with_failing_link):
        # power runs first, so this is the case that would have taken
        # the whole screen down under a single pass.
        out, _ = run_with_failing_link("power")
        assert "power" in out["screening"]["link_failures"]
        assert out["screening"]["links_screened"] == vera.SCREENING_ORDER

    def test_the_failure_is_named_with_its_reason(self, run_with_failing_link):
        out, _ = run_with_failing_link("networking")
        assert "link exploded" in out["screening"]["link_failures"]["networking"]

    def test_the_lost_names_show_up_as_no_read_not_as_absent(
            self, run_with_failing_link):
        # `not_returned` already meant "asked for a read and didn't get
        # one". A failed link's names land there rather than vanishing,
        # so `best_conviction` is not read as a quiet day.
        out, _ = run_with_failing_link("power")
        for ticker in THEME_LINKS["power"]["tickers"]:
            assert ticker in out["screening"]["not_returned"]

    def test_a_clean_run_records_no_failures(self, run_with_failing_link):
        out, _ = run_with_failing_link("no_such_link")
        assert out["screening"]["link_failures"] == {}


class TestAFailedScreenDoesNotDeleteEarlierWork:
    """FOUND IN REVIEW, and it is a consequence of the containment
    above rather than of anything that was wrong before.

    The persistence block clears today's new_candidates rows before
    reinserting, so a same-day rerun replaces rather than accumulates.
    That was only safe because a screening failure RAISED out of run()
    and never reached the delete. Per-link screening contains those
    failures, so a pass in which every link failed would arrive at the
    delete with an empty candidate list and wipe a successful earlier
    run's rows — leaving Solomon's morning meeting with no candidates
    and nothing in the log saying why.
    """

    def deletes_issued(self, monkeypatch, screened, batches_nonempty=True):
        """Run the persistence block and report which NewCandidate
        deletes it issued."""
        from contextlib import contextmanager

        from tests.test_vera import FakeQuery

        issued = []

        class RecordingSession:
            def query(self, model, *rest):
                if model is vera.NewCandidate:
                    issued.append(model)
                if model is vera.Position:
                    return FakeQuery([])
                return FakeQuery([])

            def execute(self, stmt):
                pass

            def add(self, obj):
                pass

        @contextmanager
        def scope():
            yield RecordingSession()

        def fake_screen(link, tickers, today, atlas):
            if screened is None:
                raise RuntimeError("link exploded")
            return [s for s in screened if s.ticker in tickers]

        monkeypatch.setattr(vera, "session_scope", scope)
        monkeypatch.setattr(vera, "get_open_theses", lambda s: [])
        monkeypatch.setattr(
            vera, "get_position_tickers",
            lambda s: list(THEME_UNIVERSE) if not batches_nonempty else [])
        monkeypatch.setattr(vera, "review_orphan_positions", lambda d: [])
        monkeypatch.setattr(vera, "screen_link", fake_screen)
        vera.run(TODAY, {"regime_signal": "risk_on"})
        return issued

    def test_a_totally_failed_screen_deletes_nothing(self, monkeypatch):
        assert self.deletes_issued(monkeypatch, screened=None) == []

    def test_nothing_left_to_screen_deletes_nothing(self, monkeypatch):
        assert self.deletes_issued(monkeypatch, screened=[],
                                   batches_nonempty=False) == []

    def test_a_successful_screen_still_replaces_todays_rows(self, monkeypatch):
        from agents.vera import NewCandidateOutput

        read = [NewCandidateOutput(ticker="GEV", thesis="backlog converting",
                                   conviction_score=2)]
        assert self.deletes_issued(monkeypatch, screened=read) != []


# =================================================================
# 4. The data path — what made the widening possible
# =================================================================
class TestTheDataPath:
    """Widening to 27 names was blocked by WHERE the data came from,
    not by the list. FMP's /quote and /key-metrics are restricted to a
    fixed sample list and return 402 for anything outside it, and the
    old six names happened to be on that list."""

    def test_quotes_come_from_alpaca_not_fmp(self, monkeypatch):
        called = {}

        def fake_quote(ticker):
            called["ticker"] = ticker
            return {"mid": 101.5, "spread_pct": 0.02}

        monkeypatch.setattr(
            "core.clients.alpaca_client.get_latest_quote", fake_quote)
        monkeypatch.setattr(vera.fmp_client, "get_stock_quote",
                            lambda t: pytest.fail("FMP quote must not be called"))
        out = vera.execute_tool("get_stock_quote", {"ticker": "CRDO"})
        assert out["mid"] == 101.5
        assert called["ticker"] == "CRDO"

    def test_a_quote_failure_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(
            "core.clients.alpaca_client.get_latest_quote",
            lambda t: (_ for _ in ()).throw(RuntimeError("feed down")))
        out = vera.execute_tool("get_stock_quote", {"ticker": "MOD"})
        assert "feed down" in out["error"]

    def test_the_snapshot_keeps_fmp_for_sector_only(self, monkeypatch):
        monkeypatch.setattr(vera.fmp_client, "get_company_profile",
                            lambda t: {"sector": "Industrials", "marketCap": 1e10})
        monkeypatch.setattr(vera, "_fundamentals", lambda t: {"available": False})
        vera._sector_cache.pop("PWR", None)
        out = vera.execute_tool("get_company_snapshot", {"ticker": "PWR"})
        assert out["profile"]["sector"] == "Industrials"
        # Sector is what Nora's max_sector_pct is enforced on, so it
        # must still be harvested from the API rather than the model.
        assert vera._sector_cache["PWR"] == "Industrials"

    def test_key_metrics_is_no_longer_called(self, monkeypatch):
        # It 402s outside the sample list, which is the whole reason
        # the universe could not grow.
        monkeypatch.setattr(vera.fmp_client, "get_company_profile",
                            lambda t: {"sector": "Technology"})
        monkeypatch.setattr(vera, "_fundamentals", lambda t: {"available": True})
        monkeypatch.setattr(
            vera.fmp_client, "get_company_snapshot",
            lambda t: pytest.fail("the paywalled snapshot must not be called"))
        vera.execute_tool("get_company_snapshot", {"ticker": "VRT"})

    def test_fundamentals_come_from_stored_filings(self, monkeypatch):
        monkeypatch.setattr(vera.fmp_client, "get_company_profile",
                            lambda t: {"sector": "Technology"})
        monkeypatch.setattr(
            "core.fundamentals.fundamentals_for",
            lambda t: SimpleNamespace(
                measurable=True, years=[2022, 2023, 2024],
                share_count_change_pct=-2.5, unavailable="",
                describe=lambda: "KLAC — 2022 to 2024",
                trend=lambda name: 1.5))
        out = vera.execute_tool("get_company_snapshot", {"ticker": "KLAC"})
        assert out["fundamentals"]["available"] is True
        assert out["fundamentals"]["fiscal_years"] == [2022, 2023, 2024]
        assert out["fundamentals"]["diluted_share_change_pct"] == -2.5

    def test_a_filer_with_no_us_gaap_facts_says_so_rather_than_zeroing(
            self, monkeypatch):
        # TSM and ASML file 20-F under IFRS, so the us-gaap concepts
        # this desk tracks return nothing for them. That is an honest
        # absence and must not read as zero margins.
        monkeypatch.setattr(vera.fmp_client, "get_company_profile",
                            lambda t: {"sector": "Technology"})
        monkeypatch.setattr(
            "core.fundamentals.fundamentals_for",
            lambda t: SimpleNamespace(
                measurable=False, years=[], share_count_change_pct=None,
                unavailable="nothing stored for this ticker",
                describe=lambda: "TSM: no annual figures on record",
                trend=lambda name: None))
        out = vera.execute_tool("get_company_snapshot", {"ticker": "TSM"})
        fundamentals = out["fundamentals"]
        assert fundamentals["available"] is False
        assert "nothing stored" in fundamentals["reason"]
        assert "trends_pp" not in fundamentals

    def test_an_unsynced_name_does_NOT_count_as_a_data_failure(self, monkeypatch):
        # FOUND IN REVIEW, and it would have refused every trade for
        # the first days after this shipped. Solomon's coverage_gate
        # stands down risk-increasing proposals below 60%; the
        # backfill fills six names a session by design, so recording
        # an unsynced name as degraded would report 22% on day one.
        # Coverage answers "did the request come back", not "was the
        # answer rich enough".
        vera.fmp_client.coverage.reset()
        monkeypatch.setattr(
            "core.fundamentals.fundamentals_for",
            lambda t: SimpleNamespace(
                measurable=False, years=[], share_count_change_pct=None,
                unavailable="nothing stored for this ticker",
                describe=lambda: "x", trend=lambda n: None))
        vera._fundamentals("ASML")
        summary = vera.fmp_client.coverage.summary()
        assert summary["degraded_tickers"] == {}
        assert summary["coverage_pct"] == 100.0

    def test_a_lookup_that_RAISES_is_still_a_data_failure(self, monkeypatch):
        vera.fmp_client.coverage.reset()
        monkeypatch.setattr(
            "core.fundamentals.fundamentals_for",
            lambda t: (_ for _ in ()).throw(RuntimeError("db down")))
        out = vera._fundamentals("ANET")
        assert "db down" in out["error"]
        assert "ANET" in vera.fmp_client.coverage.summary()["degraded_tickers"]

    def test_the_profile_is_fetched_once_per_ticker_per_run(self, monkeypatch):
        # FMP's free tier allows 250 requests a day and the model may
        # call a tool on the same name more than once inside a link's
        # loop. An exhausted quota raises, which would fail every
        # remaining link.
        calls = []

        def counting_profile(ticker):
            calls.append(ticker)
            return {"sector": "Utilities"}

        monkeypatch.setattr(vera.fmp_client, "get_company_profile", counting_profile)
        monkeypatch.setattr(vera, "_fundamentals", lambda t: {"available": False})
        vera._profile_cache.clear()
        for _ in range(4):
            vera.execute_tool("get_company_snapshot", {"ticker": "VST"})
        assert calls == ["VST"]

    def test_quotes_are_NOT_cached(self, monkeypatch):
        # A price is the one thing here that is supposed to be live.
        calls = []

        def counting_quote(ticker):
            calls.append(ticker)
            return {"mid": 1.0}

        monkeypatch.setattr(
            "core.clients.alpaca_client.get_latest_quote", counting_quote)
        vera.execute_tool("get_stock_quote", {"ticker": "VST"})
        vera.execute_tool("get_stock_quote", {"ticker": "VST"})
        assert calls == ["VST", "VST"]

    def test_all_three_sources_report_into_one_coverage_count(self, monkeypatch):
        # Solomon gates on data_coverage. It only keeps meaning "how
        # much of what she asked for did she get" if the two new
        # sources record alongside the FMP one.
        vera.fmp_client.coverage.reset()
        monkeypatch.setattr(
            "core.clients.alpaca_client.get_latest_quote",
            lambda t: {"mid": 10.0})
        monkeypatch.setattr(
            "core.fundamentals.fundamentals_for",
            lambda t: SimpleNamespace(
                measurable=True, years=[2024], share_count_change_pct=None,
                unavailable="", describe=lambda: "x", trend=lambda n: None))
        monkeypatch.setattr(vera.fmp_client, "get_company_profile",
                            lambda t: {"sector": "Utilities"})

        vera.execute_tool("get_stock_quote", {"ticker": "CEG"})
        vera.execute_tool("get_company_snapshot", {"ticker": "CEG"})
        summary = vera.fmp_client.coverage.summary()
        assert summary["tickers_attempted"] >= 1
        assert "CEG" not in summary["degraded_tickers"]


# =================================================================
# 5. The theme map itself is coherent
# =================================================================
class TestTheChain:

    def test_every_name_belongs_to_exactly_one_link(self):
        # THEME_OF is what Nora's concentration aggregation will count
        # on. A name in two links would be counted twice and the book
        # would read as less concentrated than it is.
        assert set(THEME_OF) == set(THEME_UNIVERSE)
        for ticker in THEME_UNIVERSE:
            owners = [l for l, s in THEME_LINKS.items()
                      if ticker in s["tickers"]]
            assert THEME_OF[ticker] == owners[0]

    def test_the_hyperscalers_are_the_payers_link(self):
        from core.theme import HYPERSCALERS
        assert set(HYPERSCALERS) == set(THEME_LINKS["hyperscaler"]["tickers"])

    def test_memory_is_honestly_one_name(self):
        # Not a diversified sector exposure. The module says so; this
        # makes it fail loudly if someone adds a name without revisiting
        # the comment that warns about it.
        assert THEME_LINKS["memory"]["tickers"] == ["MU"]
