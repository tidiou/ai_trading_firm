"""
Atlas's output contract — and specifically the read-across boundary.

WHAT THESE TESTS PROTECT. Atlas's brief is handed to Vera as context.
If a forecast about a named stock reaches her, her screen stops being
an independent opinion and starts being a confirmation of whatever
Atlas said — and nothing downstream would look wrong. The candidate
would have a thesis, a conviction score and a provenance chain. It
would simply have been decided by the wrong agent.

So the boundary is a schema validation rather than a request in a
prompt, and this file is where the schema is held to it. Every case
below is one the prompt asks for politely and the contract enforces:

  - a ticker outside the fixed bellwether map
  - a causal edge the map does not contain
  - forward-looking or recommendation language in an event
  - a ticker anywhere in the prose fields

No database, no model call, no network — AtlasOutput is a pure
Pydantic contract, which is the whole reason it can be tested at all.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from agents.atlas import AtlasOutput, ReadAcross, build_system_prompt
from core.theme import BELLWETHER_TICKERS

TODAY = date(2026, 9, 17)


def brief(**over) -> dict:
    fields = dict(
        date=TODAY,
        regime_signal="neutral",
        key_events_today=["CPI print at 08:30 ET"],
        notable_overnight_moves=["Nasdaq futures -0.4%"],
        change_from_yesterday="minor",
        confidence="medium",
        narrative="Volatility compressed and breadth narrowed into the close.",
        read_across=[],
    )
    fields.update(over)
    return fields


def entry(**over) -> dict:
    fields = dict(ticker="NVDA", event="Q3 earnings scheduled Wednesday",
                  links_affected=["compute", "memory"], observed_move_pct=None)
    fields.update(over)
    return fields


# =================================================================
# The contract still accepts an ordinary day
# =================================================================
class TestTheOrdinaryDay:

    def test_a_brief_with_no_read_across_validates(self):
        # The common case. Most days no bellwether reports and nothing
        # moved enough to attribute, and an empty list is a real
        # answer rather than a gap.
        out = AtlasOutput.model_validate(brief())
        assert out.read_across == []

    def test_read_across_defaults_to_empty_rather_than_being_required(self):
        payload = brief()
        del payload["read_across"]
        assert AtlasOutput.model_validate(payload).read_across == []

    def test_a_valid_read_across_entry_validates(self):
        out = AtlasOutput.model_validate(brief(read_across=[entry()]))
        assert out.read_across[0].ticker == "NVDA"
        assert out.read_across[0].links_affected == ["compute", "memory"]

    def test_an_observed_move_is_carried(self):
        out = AtlasOutput.model_validate(brief(read_across=[
            entry(ticker="TSM", event="monthly revenue released",
                  links_affected=["compute"], observed_move_pct=-4.1)]))
        assert out.read_across[0].observed_move_pct == -4.1

    def test_not_observed_is_none_and_not_zero(self):
        # "We did not look" and "it did not move" are different
        # claims. A zero here would read as the second.
        out = AtlasOutput.model_validate(brief(read_across=[entry()]))
        assert out.read_across[0].observed_move_pct is None


# =================================================================
# The model cannot nominate a name
# =================================================================
class TestTickerMustBeAKnownBellwether:

    def test_a_ticker_outside_the_map_is_rejected(self):
        # AMD is in the universe and is NOT a bellwether. That is the
        # interesting case: a plausible, on-theme name that the map
        # does not assign any causal edges to.
        with pytest.raises(ValidationError) as e:
            ReadAcross.model_validate(entry(ticker="AMD",
                                            links_affected=["compute"]))
        assert "not in the bellwether map" in str(e.value)

    def test_a_ticker_outside_the_universe_entirely_is_rejected(self):
        with pytest.raises(ValidationError):
            ReadAcross.model_validate(entry(ticker="TSLA",
                                            links_affected=["compute"]))

    def test_the_error_names_what_is_permitted(self):
        # A validation failure Atlas can act on in a retry, rather
        # than one he has to guess at.
        with pytest.raises(ValidationError) as e:
            ReadAcross.model_validate(entry(ticker="AMD",
                                            links_affected=["compute"]))
        for ticker in BELLWETHER_TICKERS:
            assert ticker in str(e.value)

    def test_lowercase_is_normalised_rather_than_rejected(self):
        assert ReadAcross.model_validate(entry(ticker="nvda")).ticker == "NVDA"


# =================================================================
# The model cannot invent a causal edge
# =================================================================
class TestLinksMustComeFromTheMap:

    def test_an_edge_the_map_does_not_contain_is_rejected(self):
        # GEV reads across to power and cooling. Claiming it steers
        # memory is a causal assertion, and those are code changes
        # with tests, not morning opinions.
        with pytest.raises(ValidationError) as e:
            ReadAcross.model_validate(entry(
                ticker="GEV", event="Q3 order intake",
                links_affected=["power", "memory"]))
        assert "does not read across to ['memory']" in str(e.value)

    def test_a_subset_of_the_declared_edges_is_fine(self):
        # Reporting fewer links than the map allows is a judgement
        # about today, which is his to make.
        out = ReadAcross.model_validate(entry(
            ticker="NVDA", links_affected=["memory"]))
        assert out.links_affected == ["memory"]

    def test_a_nonexistent_link_name_is_rejected(self):
        with pytest.raises(ValidationError):
            ReadAcross.model_validate(entry(links_affected=["semiconductors"]))

    def test_naming_a_ticker_with_no_link_is_rejected(self):
        # A ticker with no affected link is a company mentioned for no
        # stated reason, which is the thing the old prohibition
        # existed to prevent.
        with pytest.raises(ValidationError) as e:
            ReadAcross.model_validate(entry(links_affected=[]))
        assert "no affected link" in str(e.value)


# =================================================================
# The model cannot forecast
# =================================================================
class TestEventMustNotForecast:

    @pytest.mark.parametrize("bad_event", [
        "guidance looks strong, should support memory",
        "likely to reprice the link",
        "we expect a beat",
        "cheap into the print",
        "bullish setup",
        "clear upside from here",
        "a tailwind for cooling",
        "memory benefits from this",
    ])
    def test_forward_looking_language_fails_the_run(self, bad_event):
        with pytest.raises(ValidationError) as e:
            ReadAcross.model_validate(entry(event=bad_event))
        assert "forward-looking or recommendation language" in str(e.value)

    @pytest.mark.parametrize("ok_event", [
        "Q3 earnings scheduled Wednesday",
        "monthly revenue released",
        "backlog fell 8% quarter on quarter",
        "capex guidance raised",
        "two announced projects cancelled",
        "bookings declined",
    ])
    def test_reporting_what_happened_passes(self, ok_event):
        # Reporting is the job. Only forecasts and recommendations are
        # out of bounds — if past-tense reporting failed here, the
        # field would be useless.
        assert ReadAcross.model_validate(entry(event=ok_event))

    def test_an_empty_event_is_rejected(self):
        with pytest.raises(ValidationError) as e:
            ReadAcross.model_validate(entry(event="   "))
        assert "mentioned for no stated reason" in str(e.value)

    def test_the_failure_is_a_rejection_not_a_silent_strip(self):
        # The alternative design — quietly deleting the offending
        # words — would leave a mangled event that read as factual.
        # Failing the run makes Atlas retry with the rule restated.
        with pytest.raises(ValidationError):
            AtlasOutput.model_validate(brief(read_across=[
                entry(event="Q3 earnings; should lift the link")]))


# =================================================================
# The prose fields keep the original prohibition
# =================================================================
class TestProseStaysTickerFree:

    def test_a_ticker_in_the_narrative_is_rejected(self):
        with pytest.raises(ValidationError) as e:
            AtlasOutput.model_validate(brief(
                narrative="The complex led, with NVDA up 4%."))
        assert "in the narrative" in str(e.value)

    def test_the_prohibition_is_described_as_moved_not_lifted(self):
        # The error should teach the rule, because the model reads it
        # on retry.
        with pytest.raises(ValidationError) as e:
            AtlasOutput.model_validate(brief(narrative="MU fell hard."))
        assert "moved, not lifted" in str(e.value)

    def test_a_ticker_in_key_events_is_rejected(self):
        with pytest.raises(ValidationError):
            AtlasOutput.model_validate(brief(
                key_events_today=["NVDA earnings after the close"]))

    def test_a_ticker_in_notable_moves_is_rejected(self):
        # This is where a bellwether move would most naturally be
        # written, which is exactly why it has to be closed — there
        # must be one auditable place, not two.
        with pytest.raises(ValidationError) as e:
            AtlasOutput.model_validate(brief(
                notable_overnight_moves=["TSM -4.1% overnight"]))
        assert "read_across" in str(e.value)

    def test_index_and_volatility_prose_is_unaffected(self):
        assert AtlasOutput.model_validate(brief(
            narrative="Breadth narrowed while the VIX held under 15.",
            notable_overnight_moves=["Nasdaq futures -0.4%", "VIX +1.2"]))

    def test_a_ticker_inside_a_longer_word_is_not_a_false_positive(self):
        # "MU" inside "MUCH" would make almost every brief
        # unpublishable if this were a substring check.
        assert AtlasOutput.model_validate(brief(
            narrative="MUCH of the move was mechanical rather than news-driven."))


# =================================================================
# The prompt and the validator read from one source
# =================================================================
class TestPromptConsistency:

    def test_the_prompt_lists_every_permitted_ticker(self):
        # If the prompt and BELLWETHERS disagreed, Atlas would be
        # instructed to name something the contract rejects, and every
        # run would fail validation for a reason nobody could see.
        prompt = build_system_prompt()
        for ticker in BELLWETHER_TICKERS:
            assert ticker in prompt, ticker

    def test_the_prompt_states_both_hard_rules(self):
        prompt = build_system_prompt()
        assert "Tickers appear ONLY in read_across" in prompt
        # Asserted on the unwrapped fragment: the prompt is a wrapped
        # literal, so matching a phrase that spans a line break makes
        # the test fail on reformatting rather than on meaning.
        assert "You cannot add a" in prompt
        assert "name or an edge" in prompt

    def test_the_prompt_is_honest_about_having_no_news_feed(self):
        # The input gap is real and must stay visible in the
        # instructions, or Atlas fills it with plausible invention.
        prompt = build_system_prompt()
        assert "do NOT have a news feed" in prompt
        assert "cannot report WHY" in prompt

    def test_the_json_example_survives_formatting(self):
        # The prompt uses .format() to inject the map, so every literal
        # brace in the JSON example has to be doubled. Get that wrong
        # and the example renders as a KeyError or as mangled JSON.
        prompt = build_system_prompt()
        assert '"regime_signal": "risk-on | risk-off | neutral | transitioning"' in prompt
        assert '"links_affected": ["compute", "memory"]' in prompt
        assert "{{" not in prompt and "}}" not in prompt
