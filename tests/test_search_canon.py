"""``search(q=..., args={'canon': True})`` — opt-in canon pre-pass (gr548267).

A query token that is a taxon alias ('TOF') must reach a chunk that only says
the canonical term ('turnover frequency') on the lexical leg. Off by default:
without ``canon`` the output is byte-identical to a call that never heard of it.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.utils import canon
from precis.utils.canon import CanonHit, CanonState

_EMB = MockEmbedder(dim=1024)


@pytest.fixture
def runtime(runtime_with_store: PrecisRuntime) -> PrecisRuntime:
    return runtime_with_store


def _taxon(store: Store, name: str, aliases: list[str] | None = None) -> int:
    from precis.dispatch import Hub
    from precis.handlers.taxon import TaxonHandler

    resp = TaxonHandler(hub=Hub(store=store)).put(
        text=f"{name} — a quantity used in the canon tests. It has a second sentence",
        meta={"aliases": aliases} if aliases else None,
        dedup=False,
    )
    m = re.search(r"\btn(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _memory(store: Store, text: str) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=text[:40])
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=text, embedding=_EMB.embed_one(text))]
    )
    return ref.id


def _search(
    rt: PrecisRuntime, q: str, *, canon_on: bool | None = True, **kw: Any
) -> str:
    args: dict[str, Any] = {"kind": "memory", "q": q, **kw}
    if canon_on is not None:
        args["__extras__"] = {"canon": canon_on}
    return rt.dispatch("search", args)


def _hit(ref_id: int, out: str) -> bool:
    return f"memory {ref_id}:" in out


class TestExpansion:
    def test_alias_reaches_the_canonical_term_and_the_line_renders(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        tn = _taxon(store, "turnover frequency", ["TOF"])
        me = _memory(store, "platinum turnover frequency at 300 K")
        off = _search(runtime, "TOF", canon_on=None)
        on = _search(runtime, "TOF")
        assert not _hit(me, off) and "canon:" not in off
        assert _hit(me, on), on
        assert on.splitlines()[0] == f"canon: TOF → tn{tn} turnover frequency"

    def test_canon_false_is_byte_identical_to_omitted(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        _taxon(store, "turnover frequency", ["TOF"])
        _memory(store, "platinum turnover frequency at 300 K")
        _memory(store, "TOF written out in the note")
        omitted = _search(runtime, "TOF", canon_on=None)
        assert _search(runtime, "TOF", canon_on=False) == omitted
        assert "canon:" not in omitted

    def test_expansion_keeps_the_other_query_words_anded(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        _taxon(store, "turnover frequency", ["TOFR"])
        both = _memory(store, "platinum turnover frequency at 300 K")
        only_expansion = _memory(store, "gold turnover frequency at 300 K")
        out = _search(runtime, "TOFR platinum")
        assert _hit(both, out) and not _hit(only_expansion, out), out

    def test_ambiguous_alias_expands_nothing(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        a = _taxon(store, "alpha ambiguity term", ["AMBG"])
        b = _taxon(store, "beta ambiguity term", ["AMBG"])
        me = _memory(store, "the alpha ambiguity term is defined here")
        out = _search(runtime, "AMBG")
        assert not _hit(me, out)
        assert f"canon: 'AMBG' is ambiguous (tn{a}, tn{b}) — not expanded" in out

    def test_short_alias_expands_only_for_a_single_token_query(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        _taxon(store, "photo voltaic cell", ["PV"])
        me = _memory(store, "photo voltaic cell efficiency record")
        assert _hit(me, _search(runtime, "PV"))
        multi = _search(runtime, "PV efficiency")
        assert not _hit(me, multi)
        assert "canon: no unique taxon/alias match in q" in multi

    def test_alias_row_resolves_through_its_taxon(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        from precis.dispatch import Hub
        from precis.handlers.alias import AliasHandler

        tn = _taxon(store, "oxygen reduction reaction")
        AliasHandler(hub=Hub(store=store)).put(
            a="ORR", b="oxygen reduction reaction", rel="short-for"
        )
        me = _memory(store, "kinetics of the oxygen reduction reaction on Pt")
        out = _search(runtime, "ORR")
        assert _hit(me, out), out
        assert f"canon: ORR → tn{tn} oxygen reduction reaction" in out

    def test_see_also_alias_rows_are_not_synonyms(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        from precis.dispatch import Hub
        from precis.handlers.alias import AliasHandler

        _taxon(store, "hydrogen evolution reaction")
        AliasHandler(hub=Hub(store=store)).put(
            a="HERX", b="hydrogen evolution reaction", rel="see-also"
        )
        me = _memory(store, "hydrogen evolution reaction on nickel")
        out = _search(runtime, "HERX")
        assert not _hit(me, out)
        assert "no unique taxon/alias match" in out

    def test_case_insensitive_lookup(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        tn = _taxon(store, "turnover frequency", ["TOF"])
        me = _memory(store, "platinum turnover frequency at 300 K")
        assert _hit(me, _search(runtime, "Tof"))
        out = _search(runtime, "TURNOVER  Frequency")
        assert f"canon: TURNOVER Frequency → tn{tn} turnover frequency" in out

    def test_quoted_phrase_is_never_expanded(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        _taxon(store, "turnover frequency", ["TOF"])
        me = _memory(store, "platinum turnover frequency at 300 K")
        out = _search(runtime, '"TOF"')
        assert not _hit(me, out)
        assert "no unique taxon/alias match" in out

    def test_instance_of_refs_get_a_bonus_not_a_filter(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        tn = _taxon(store, "turnover frequency", ["TOF"])
        member = _memory(store, "member note on turnover frequency")
        other = _memory(store, "other note on turnover frequency")
        store.add_link(src_ref_id=member, dst_ref_id=tn, relation="instance-of")
        out = _search(runtime, "TOF")
        assert _hit(member, out) and _hit(other, out), out

    def test_three_char_alias_is_skipped_inside_a_longer_query(
        self, store: Store, runtime: PrecisRuntime
    ) -> None:
        _taxon(store, "turnover frequency", ["TOF"])
        me = _memory(store, "platinum turnover frequency at 300 K")
        out = _search(runtime, "TOF platinum")
        assert not _hit(me, out)
        assert "canon: no unique taxon/alias match in q" in out


def test_canon_line_flags_an_unused_expansion() -> None:
    from precis.runtime.canon import canon_line

    state = CanonState(q="TOF", hits=[CanonHit("TOF", 7, "turnover frequency", ())])
    assert canon_line(state) == (
        "canon: TOF → tn7 turnover frequency (no lexical leg of this search used it)"
    )
    state.applied = True
    assert canon_line(state) == "canon: TOF → tn7 turnover frequency"
    assert canon_line(CanonState(q="")) == "canon: no q="


class TestRefusals:
    @pytest.mark.parametrize("mode", ["lexical", "verbatim", "literal"])
    def test_exact_modes_refuse_canon(self, runtime: PrecisRuntime, mode: str) -> None:
        out = _search(runtime, "TOF", mode=mode)
        assert "canon=True does not combine with mode=" in out
        assert "drop mode=" in out

    def test_non_boolean_is_refused_with_options(self, runtime: PrecisRuntime) -> None:
        out = runtime.dispatch(
            "search", {"kind": "memory", "q": "x", "__extras__": {"canon": "yes"}}
        )
        assert "canon= must be true or false" in out and "canon': True" in out

    def test_passed_twice_is_refused(self, runtime: PrecisRuntime) -> None:
        out = runtime.dispatch(
            "search",
            {"kind": "memory", "q": "x", "canon": True, "__extras__": {"canon": True}},
        )
        assert "canon= was passed twice" in out


class TestFusedBonus:
    def test_fused_score_gains_exactly_the_bonus_for_boosted_refs(
        self, store: Store
    ) -> None:
        boosted = _memory(store, "catalyst turnover note alpha")
        plain = _memory(store, "catalyst turnover note beta")
        qv = _EMB.embed_one("catalyst turnover")

        def scores() -> dict[int, float]:
            return {
                ref.id: s
                for _b, ref, s in store.chunks.search_chunks_fused(
                    q="catalyst", query_vec=qv
                )
            }

        base = scores()
        token = canon.bind(CanonState(q="x", boost_ids=(boosted,)))
        try:
            lifted = scores()
        finally:
            canon.unbind(token)
        assert lifted[boosted] == pytest.approx(base[boosted] + canon.RANK_BONUS)
        assert lifted[plain] == pytest.approx(base[plain])
        assert scores() == base

    def test_bonus_param_stays_aligned_with_fold_and_floor_params(
        self, store: Store
    ) -> None:
        boosted = _memory(store, "catalyst turnover note alpha")
        _memory(store, "catalyst turnover note beta")
        qv = _EMB.embed_one("catalyst turnover")
        token = canon.bind(CanonState(q="x", boost_ids=(boosted,)))
        fold: dict[int, tuple[float | None, bool]] = {}
        try:
            rows = store.chunks.search_chunks_fused(
                q="catalyst turnover",
                query_vec=qv,
                semantic_only_max_distance=0.99,
                fold_info_out=fold,
            )
        finally:
            canon.unbind(token)
        assert {r.id for _b, r, _s in rows} >= {boosted}
        assert len(fold) == len(rows)

    def test_no_state_leaves_q_untouched(self) -> None:
        assert canon.lexical_q("TOF of catalyst") == "TOF of catalyst"
        assert canon.boost_ref_ids() is None


HIT = CanonHit("TOFR", 1, "turnover frequency", ("turnover frequency", "TOFR", "rate"))


class TestRewrite:
    def test_distributes_over_the_other_words(self) -> None:
        out = canon.rewrite("TOFR of catalyst", [HIT])
        assert out == (
            'TOFR of catalyst or "turnover frequency" of catalyst or rate of catalyst'
        )

    def test_never_rewrites_inside_quotes_or_negation(self) -> None:
        assert canon.rewrite('"TOFR" catalyst', [HIT]) == '"TOFR" catalyst'
        assert canon.rewrite("catalyst -TOFR", [HIT]) == "catalyst -TOFR"
        assert canon.rewrite('"unclosed TOFR', [HIT]) == '"unclosed TOFR'

    def test_punctuation_around_the_token_survives(self) -> None:
        out = canon.rewrite("(TOFR), catalyst", [HIT])
        assert out.split(" or ")[1].startswith('("turnover frequency"),')

    def test_short_surface_only_alone(self) -> None:
        short = CanonHit("PV", 2, "photovoltaic", ("photovoltaic",))
        assert canon.rewrite("PV", [short]) == "PV or photovoltaic"
        assert canon.rewrite("PV cell", [short]) == "PV cell"

    def test_two_gram_consumes_its_tokens(self) -> None:
        two = CanonHit("oxygen reduction", 3, "ORR", ("ORR",))
        one = CanonHit("oxygen", 4, "O2", ("O2",))
        assert canon.rewrite("oxygen reduction kinetics", [one, two]) == (
            "oxygen reduction kinetics or ORR kinetics"
        )

    def test_branch_cap_drops_the_expansion_not_the_query(self) -> None:
        wide = CanonHit("TOFR", 1, "x", tuple(f"t{i}" for i in range(30)))
        assert canon.rewrite("TOFR", [wide]) == "TOFR"

    def test_surfaces_prefer_two_grams_and_skip_quotes(self) -> None:
        got = [s.key for s in canon.surfaces('oxygen reduction "no go" cell')]
        assert got[:1] == ["oxygen reduction"] and "no go" not in " ".join(got)

    def test_expansion_is_valid_websearch_syntax(self, store: Store) -> None:
        sql = "SELECT websearch_to_tsquery('english', %s)::text"
        with store.pool.connection() as conn:
            got = conn.execute(
                sql, (canon.rewrite("TOFR of catalyst", [HIT]),)
            ).fetchone()
        assert got is not None
        assert "'turnov' <-> 'frequenc' & 'catalyst'" in got[0]
        assert "'tofr' & 'catalyst'" in got[0]


def test_refusal_helpers_raise_badinput_with_next() -> None:
    from precis.runtime.canon import pop_canon_flag, refuse_exact_mode

    with pytest.raises(BadInput) as ei:
        pop_canon_flag({"canon": 3})
    assert ei.value.next
    with pytest.raises(BadInput) as ei2:
        refuse_exact_mode("Lexical")
    assert ei2.value.next
    assert pop_canon_flag({"__extras__": {"canon": True}}) is True
    assert pop_canon_flag({}) is False
