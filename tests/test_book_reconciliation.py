"""
Positions against open theses — the invariant nobody was checking.

THE DEFECT THIS LOCKS DOWN. Vera's monitoring pass took its list of
held names from the OPEN THESES table and never compared it to what
the broker said was owned. On 2026-09-23 the book held AAPL and NVDA
while she monitored AAPL and GOOGL:

  GOOGL  a thesis opened at ALLOCATION on 17 Sept. Ada refused the
         order on a stale ledger, so nothing filled — and the thesis
         outlived the order. Its assumptions were checked daily
         against a position that did not exist, and Clara published
         "AAPL and GOOGL holdings are intact".
  NVDA   84% of the invested book, monitored by nobody, because the
         orphan-adoption pass that documents a held name ran AFTER
         the monitoring pass had already chosen its list.

WHY IT SURVIVED SIX DAYS, and why these tests are about NAMES: both
counts were 2. Nora said "2 positions" from the positions table, Vera
said "2 positions monitored" from the theses table, the numbers
agreed, and nothing ever printed the two lists side by side. Every
assertion below that could have been written against a count is
written against a set of tickers instead. A test that only checked
len() would have passed on 17 September.

The reconciliation is a pure function over two ticker lists precisely
so this file needs no database.
"""

from agents.vera import BookReconciliation, reconcile_book


# =================================================================
# 1. The three sets
# =================================================================
class TestTheThreeSets:

    def test_a_matched_book_is_all_monitored(self):
        r = reconcile_book(["AAPL", "NVDA"], ["AAPL", "NVDA"])
        assert r.monitored == ["AAPL", "NVDA"]
        assert r.orphans == []
        assert r.phantoms == []

    def test_a_position_without_a_thesis_is_an_orphan(self):
        r = reconcile_book(["AAPL", "NVDA"], ["AAPL"])
        assert r.orphans == ["NVDA"]
        assert r.phantoms == []

    def test_a_thesis_without_a_position_is_a_phantom(self):
        r = reconcile_book(["AAPL"], ["AAPL", "GOOGL"])
        assert r.phantoms == ["GOOGL"]
        assert r.orphans == []

    def test_THE_23_SEPT_STATE_exactly(self):
        # positions: AAPL, NVDA.  open theses: AAPL, GOOGL, NVDA.
        r = reconcile_book(["AAPL", "NVDA"], ["AAPL", "GOOGL", "NVDA"])
        assert r.monitored == ["AAPL", "NVDA"]
        assert r.phantoms == ["GOOGL"]
        assert r.orphans == []
        assert r.is_reconciled is False

    def test_THE_17_SEPT_STATE_both_differences_at_once(self):
        # positions: AAPL, NVDA.  open theses: AAPL, GOOGL. NVDA was
        # held and undocumented while GOOGL was documented and unheld
        # — the two failures are independent and were both live.
        r = reconcile_book(["AAPL", "NVDA"], ["AAPL", "GOOGL"])
        assert r.monitored == ["AAPL"]
        assert r.orphans == ["NVDA"]
        assert r.phantoms == ["GOOGL"]

    def test_the_old_held_list_and_the_new_one_are_not_the_same_list(self):
        # THE REGRESSION, stated as a set comparison. The old code used
        # the theses list as "held". On 17 Sept that list and the
        # broker's list had the same length and different contents.
        positions = ["AAPL", "NVDA"]
        theses = ["AAPL", "GOOGL"]
        assert len(positions) == len(theses)     # why it was invisible
        r = reconcile_book(positions, theses)
        assert r.positions != r.theses
        assert r.is_reconciled is False


# =================================================================
# 2. Shape guarantees the callers rely on
# =================================================================
class TestShape:

    def test_every_set_is_sorted(self):
        r = reconcile_book(["NVDA", "AAPL"], ["MSFT", "AAPL"])
        for names in (r.positions, r.theses, r.monitored, r.orphans, r.phantoms):
            assert names == sorted(names)

    def test_duplicates_collapse(self):
        # positions is keyed by ticker upstream, but a duplicated row
        # must not make one name look like two.
        r = reconcile_book(["AAPL", "AAPL"], ["AAPL"])
        assert r.positions == ["AAPL"]
        assert r.monitored == ["AAPL"]

    def test_the_three_sets_partition_the_union(self):
        # Nothing is lost and nothing is double-counted, whatever goes in.
        r = reconcile_book(["AAPL", "NVDA", "MU"], ["AAPL", "GOOGL"])
        union = set(r.positions) | set(r.theses)
        assert set(r.monitored) | set(r.orphans) | set(r.phantoms) == union
        assert len(r.monitored) + len(r.orphans) + len(r.phantoms) == len(union)

    def test_monitored_is_a_subset_of_both(self):
        r = reconcile_book(["AAPL", "NVDA"], ["AAPL", "GOOGL"])
        assert set(r.monitored) <= set(r.positions)
        assert set(r.monitored) <= set(r.theses)

    def test_an_empty_book_reconciles(self):
        r = reconcile_book([], [])
        assert r.is_reconciled is True
        assert r.monitored == []

    def test_day_one_a_thesis_before_any_fill_is_a_phantom_not_an_error(self):
        # Not a bug on its own — it is the state between approval and
        # fill. It becomes a defect when it persists, which is what
        # the daily WARNING is for.
        r = reconcile_book([], ["GOOGL"])
        assert r.phantoms == ["GOOGL"]
        assert r.is_reconciled is False

    def test_it_takes_plain_strings_not_orm_rows(self):
        # The signature is what makes this testable without a database,
        # so it is pinned.
        assert reconcile_book(("AAPL",), {"AAPL"}).monitored == ["AAPL"]


# =================================================================
# 3. describe() — the line that would have caught it
# =================================================================
class TestDescribe:

    def test_a_clean_book_says_so_and_names_what_is_monitored(self):
        s = reconcile_book(["AAPL", "NVDA"], ["AAPL", "NVDA"]).describe()
        assert "reconciled" in s
        assert "NOT" not in s
        assert "AAPL" in s and "NVDA" in s

    def test_a_phantom_is_named_not_counted(self):
        s = reconcile_book(["AAPL", "NVDA"], ["AAPL", "GOOGL", "NVDA"]).describe()
        assert "NOT reconciled" in s
        assert "GOOGL" in s          # THE point of the whole change
        assert "phantom" in s.lower()

    def test_an_orphan_is_named_not_counted(self):
        s = reconcile_book(["AAPL", "NVDA"], ["AAPL"]).describe()
        assert "NVDA" in s
        assert "orphan" in s.lower()

    def test_both_differences_appear_in_one_line(self):
        s = reconcile_book(["AAPL", "NVDA"], ["AAPL", "GOOGL"]).describe()
        assert "NVDA" in s and "GOOGL" in s
        assert "orphan" in s.lower() and "phantom" in s.lower()

    def test_the_empty_book_line_is_not_a_bare_count(self):
        s = reconcile_book([], []).describe()
        assert "none" in s
        assert s.strip()

    def test_the_two_states_do_not_render_alike(self):
        # A human skimming a log has to be able to tell them apart at
        # a glance; "2 positions monitored" was true in both.
        clean = reconcile_book(["AAPL", "NVDA"], ["AAPL", "NVDA"]).describe()
        broken = reconcile_book(["AAPL", "NVDA"], ["AAPL", "GOOGL"]).describe()
        assert clean != broken
        assert clean.startswith("Book reconciled")
        assert broken.startswith("Book NOT reconciled")


# =================================================================
# 4. The dataclass is constructible directly, for callers that
#    already have the sets (and so the field ORDER is pinned — a
#    silent reorder would swap orphans and phantoms, which are the
#    two things this change exists to keep apart).
# =================================================================
class TestFieldOrder:

    def test_positional_construction_keeps_orphans_and_phantoms_apart(self):
        r = BookReconciliation(["AAPL"], ["AAPL", "GOOGL"], ["AAPL"],
                               [], ["GOOGL"])
        assert r.positions == ["AAPL"]
        assert r.theses == ["AAPL", "GOOGL"]
        assert r.monitored == ["AAPL"]
        assert r.orphans == []
        assert r.phantoms == ["GOOGL"]

    def test_is_reconciled_needs_both_sides_empty(self):
        assert BookReconciliation([], [], [], [], []).is_reconciled is True
        assert BookReconciliation([], [], [], ["X"], []).is_reconciled is False
        assert BookReconciliation([], [], [], [], ["Y"]).is_reconciled is False
        assert BookReconciliation([], [], [], ["X"], ["Y"]).is_reconciled is False
