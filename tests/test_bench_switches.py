"""Server-side memory-ablation switches (``precis.bench.switches``, td604843).

One test per switch, each comparing a small seeded fixture's render (or
ranking) with the layer on against the same call under ``applied(...)`` —
the layer must be absent from the off render and present in the on render.
Plus: all-on is byte-identical to no switches, ``from_env`` parsing, the
dispatch seeding, and the context var resetting after ``applied()``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.bench import switches
from precis.bench.switches import ALL_ON, Switches, applied, from_env
from precis.config import PrecisConfig
from precis.dispatch import Hub, boot
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.store.types import Tag
from precis.utils import handle_registry
from precis.utils.eye_render import render_eye

# ── fixtures ─────────────────────────────────────────────────────────


def _seed_paper(store: Store, *, slug: str = "ablate01", n: int = 6) -> int:
    """A paper whose chunks all carry the keyword ``alpha`` and whose first
    chunk carries an ``llm-v1`` summary. Returns the ref id."""
    ref = store.insert_ref(kind="paper", slug=slug, title="Ablation fixture paper")
    e = MockEmbedder(dim=store.embedding_dim())
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i, text=f"body text of chunk {i}", embedding=e.embed_one(str(i))
            )
            for i in range(n)
        ],
    )
    with store.pool.connection() as conn:
        for b in blocks:
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE chunk_id = %s",
                (["alpha", "beta"], b.id),
            )
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
            "VALUES (%s, 'llm-v1', %s, 'ok')",
            (blocks[0].id, "The opening summary."),
        )
    return ref.id


def _chunk_handle(store: Store, ref_id: int, ord_: int) -> str:
    block = store.chunks.list_chunks_for_ref(ref_id)[ord_]
    return handle_registry.format_handle("paper", block.id, chunk=True)


def _embedded_memory(rt: PrecisRuntime, text: str) -> int:
    out = rt.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"id=(\d+)", out)
    assert m, out
    ref_id = int(m.group(1))
    store = rt.hub.live_store
    (cid,) = store.chunks.card_chunk_ids([ref_id])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, 'bge-m3', %s, 'ok', 1) "
            "ON CONFLICT (chunk_id, embedder) DO UPDATE "
            "SET vector = EXCLUDED.vector, status = 'ok'",
            (cid, rt.hub.embed_one(text)),
        )
    return ref_id


class _SpyEmbedder(MockEmbedder):
    """MockEmbedder that counts query embeds."""

    def __init__(self, *, dim: int) -> None:
        super().__init__(dim=dim)
        self.calls = 0

    def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        return super().embed_one(text)


# ── the module itself ────────────────────────────────────────────────


def test_default_is_all_on() -> None:
    assert switches.current() == ALL_ON
    assert ALL_ON.all_on
    assert Switches(kwd=False) != ALL_ON and not Switches(kwd=False).all_on


@pytest.mark.parametrize(
    ("raw", "flag"),
    [
        ("no-kwd", "kwd"),
        ("no-summary", "summary"),
        ("no-categorizer", "categorizer"),
        ("no-fisheye", "fisheye"),
        ("no-recall", "recall"),
        ("lexical-only", "semantic"),
        ("semantic-only", "lexical"),
    ],
)
def test_from_env_each_arm_turns_one_flag_off(raw: str, flag: str) -> None:
    sw = from_env({switches.ENV_VAR: raw})
    off = [
        f
        for f in (
            "kwd",
            "summary",
            "categorizer",
            "fisheye",
            "recall",
            "lexical",
            "semantic",
        )
        if not getattr(sw, f)
    ]
    assert off == [flag]


def test_from_env_unset_blank_and_combined() -> None:
    assert from_env({}) == ALL_ON
    assert from_env({switches.ENV_VAR: " , "}) == ALL_ON
    sw = from_env({switches.ENV_VAR: " No-KWD , semantic-only,"})
    assert (sw.kwd, sw.lexical, sw.semantic, sw.summary) == (False, False, True, True)


def test_from_env_rejects_unknown_arm_and_the_lexical_semantic_conflict() -> None:
    with pytest.raises(ValueError, match="unknown arm 'no-such'"):
        from_env({switches.ENV_VAR: "no-kwd,no-such"})
    with pytest.raises(ValueError, match="lexical-only.*semantic-only"):
        from_env({switches.ENV_VAR: "lexical-only,semantic-only"})


def test_applied_resets_the_context_var_even_on_error() -> None:
    assert switches.current() == ALL_ON
    with applied(kwd=False) as sw:
        assert sw.kwd is False and switches.current() is sw
        with applied(summary=False):
            assert (switches.current().kwd, switches.current().summary) == (
                False,
                False,
            )
        assert switches.current().summary is True  # inner restored
    assert switches.current() == ALL_ON
    with pytest.raises(RuntimeError), applied(fisheye=False):
        raise RuntimeError("boom")
    assert switches.current() == ALL_ON
    with pytest.raises(TypeError), applied(no_such_flag=False):
        pass
    assert switches.current() == ALL_ON


def test_seeded_reads_env_but_keeps_an_applied_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(switches.ENV_VAR, "no-recall")
    with switches.seeded() as sw:
        assert sw.recall is False and switches.current() is sw
    assert switches.current() == ALL_ON  # reset
    with applied(kwd=False), switches.seeded() as sw:
        assert sw.kwd is False and sw.recall is True  # applied wins; env not merged
    monkeypatch.setenv(switches.ENV_VAR, "bogus")
    with pytest.raises(ValueError), switches.seeded():
        pass


def test_search_mode_rewrites_only_the_hybrid_default() -> None:
    lex_only = Switches(semantic=False)
    sem_only = Switches(lexical=False)
    for mode in (None, "", "hybrid", "HYBRID"):
        assert lex_only.search_mode(mode) == "lexical"
        assert sem_only.search_mode(mode) == "semantic"
        assert ALL_ON.search_mode(mode) == mode  # untouched, None stays None
    for mode in ("semantic", "lexical", "verbatim", "literal"):
        assert lex_only.search_mode(mode) == mode
        assert sem_only.search_mode(mode) == mode


# ── kwd ──────────────────────────────────────────────────────────────


def test_kwd_off_removes_chunk_keywords_from_rows_and_renders(hub: Hub) -> None:
    store = hub.live_store
    ref_id = _seed_paper(store)
    pc = _chunk_handle(store, ref_id, 2)

    on = render_eye(store, pc, "verbatim")
    assert "alpha, beta" in on  # neighbour rows are their keywords
    assert store.chunks.list_chunks_for_ref(ref_id)[0].keywords == ["alpha", "beta"]

    with applied(kwd=False):
        off = render_eye(store, pc, "verbatim")
        assert "alpha" not in off and "beta" not in off
        assert "body text of chunk 1" in off  # rows fall back to the text
        assert all(b.keywords is None for b in store.chunks.list_chunks_for_ref(ref_id))
        (row,) = store.chunks.chunk_llm_summaries_for_ref(ref_id, pos_range=(0, 0))
        assert row["keywords"] == ""
        assert row["summary"] == "The opening summary."  # other layers untouched


def test_kwd_off_refuses_the_kwd_rung(hub: Hub) -> None:
    store = hub.live_store
    ref_id = _seed_paper(store)
    pa = handle_registry.format_handle("paper", ref_id)
    assert render_eye(store, pa, "kwd").startswith("· ")
    with (
        applied(kwd=False),
        pytest.raises(BadInput, match="'kwd' rung is switched off"),
    ):
        render_eye(store, pa, "kwd")


def test_kwd_off_drops_the_keyword_containment_leg_from_verbatim_ranking() -> None:
    from precis.store._chunks_ops import _verbatim_pred

    sql_on, _ = _verbatim_pred(["alpha"])
    assert "c.keywords" in sql_on
    with applied(kwd=False):
        for scoped in (False, True):
            sql_off, params = _verbatim_pred(["alpha"], scoped=scoped)
            assert "keywords" not in sql_off and "ILIKE" in sql_off
            assert params == ["%alpha%"]


def test_kwd_off_drops_the_keyword_clustering_notice_and_readiness_part(
    hub: Hub,
) -> None:
    from precis.handlers.paper import _toc_readiness_line

    store = hub.live_store
    ref_id = _seed_paper(store)
    assert "keyworded" in (_toc_readiness_line(store, ref_id) or "")
    with applied(kwd=False):
        line = _toc_readiness_line(store, ref_id) or ""
        assert "keyworded" not in line and "keywords pending" not in line
        assert "summarised" in line


# ── summary ──────────────────────────────────────────────────────────


def test_summary_off_removes_llm_summaries_and_the_summary_rung(store: Store) -> None:
    hub = Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))
    handler = PaperHandler(hub=hub)
    ref_id = _seed_paper(store)
    pa = handle_registry.format_handle("paper", ref_id)

    assert "The opening summary." in handler.get(id=pa, view="summaries").body
    assert render_eye(store, pa, "summary")
    with applied(summary=False):
        out = handler.get(id=pa, view="summaries").body
        assert "The opening summary." not in out
        assert "0 with an llm summary" in out
        assert "alpha" in out  # keywords (another layer) still there
        with pytest.raises(BadInput, match="'summary' rung is switched off"):
            render_eye(store, pa, "summary")
        assert render_eye(store, pa, "verbatim")  # the rung above still reads


# ── categorizer ──────────────────────────────────────────────────────


def test_categorizer_off_drops_axis_and_topic_chips(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Categorized note")
    for ns, value in (("DOMAIN", "other"), ("DOMAINCASCADE", "1")):
        store.add_tag(mem.id, Tag.closed(ns, value), set_by="system")
    store.add_tag(mem.id, Tag.open("topic:mof"), set_by="system")
    store.add_tag(mem.id, Tag.open("thread:keepme"), set_by="system")
    h = handle_registry.format_handle("memory", mem.id)

    head_on = render_eye(store, h, "fisheye").splitlines()[0]
    assert "[DOMAIN:other]" in head_on and "[topic:mof]" in head_on
    with applied(categorizer=False):
        head_off = render_eye(store, h, "fisheye").splitlines()[0]
        assert "DOMAIN" not in head_off and "topic:mof" not in head_off
        assert "[thread:keepme]" in head_off  # non-categorizer tags stay


def test_categorizer_off_hides_the_hint_line_and_refuses_categorizer_filters(
    runtime_with_store: PrecisRuntime,
) -> None:
    from precis.categorizers import is_categorizer_tag, ref_hint_line

    assert is_categorizer_tag("DOMAIN:chemistry") and is_categorizer_tag("topic:mof")
    assert not is_categorizer_tag("STATUS:open") and not is_categorizer_tag("mof")

    rt = runtime_with_store
    store = rt.hub.live_store
    ref = store.insert_ref(kind="paper", slug="hintme", title="Hint me")
    with applied(categorizer=False):
        assert ref_hint_line(store, ref) is None
        for key in ("tags", "exclude_tags"):
            out = rt.dispatch(
                "search", {"kind": "paper", "q": "x", key: ["DOMAIN:other"]}
            )
            assert "categorizer layer is switched off" in out, out
        out = rt.dispatch("search", {"kind": "paper", "q": "x", "tags": ["topic:mof"]})
        assert "categorizer layer is switched off" in out
        # a taxon under= is a categorizer-layer match too
        out = rt.dispatch(
            "search", {"kind": "paper", "q": "x", "under": "technique/afm"}
        )
        assert "taxon under=" in out and "switched off" in out, out
    # on: the same filter is accepted (empty result, no refusal)
    out = rt.dispatch("search", {"kind": "paper", "q": "x", "tags": ["DOMAIN:other"]})
    assert "switched off" not in out


# ── fisheye ──────────────────────────────────────────────────────────


def test_fisheye_off_collapses_fisheye_rungs_to_verbatim_and_fetches_no_hop(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils import eye_render

    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Fisheye focus")
    other = store.insert_ref(kind="memory", slug=None, title="Linked neighbour")
    store.add_link(src_ref_id=mem.id, dst_ref_id=other.id, relation="related-to")
    h = handle_registry.format_handle("memory", mem.id)

    on = render_eye(store, h, "fisheye+1hop")
    assert "— linked (1 hop) —" in on and "Linked neighbour" in on
    verbatim = render_eye(store, h, "verbatim")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a neighbourhood was fetched")

    with applied(fisheye=False):
        monkeypatch.setattr(eye_render, "_first_hop", _boom)
        monkeypatch.setattr(eye_render, "_second_hop", _boom)
        for rung in ("fisheye", "fisheye+1hop", "fisheye+2hop"):
            assert render_eye(store, h, rung) == verbatim
        assert "Linked neighbour" not in verbatim


# ── recall ───────────────────────────────────────────────────────────


def test_recall_off_skips_the_similarity_search_even_when_requested(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils import eye_render

    rt = runtime_with_store
    text = "copper catalyses nitrate reduction"
    a = _embedded_memory(rt, text)
    b = _embedded_memory(rt, text)
    store = rt.hub.live_store
    h = handle_registry.format_handle("memory", a)

    on = render_eye(store, h, "fisheye+1hop+recall")
    assert "— recall (nearest by embedding" in on and f"me{b}" in on

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the recall search ran")

    with applied(recall=False):
        monkeypatch.setattr(eye_render, "_recall", _boom)
        off = render_eye(store, h, "fisheye+1hop+recall")
        assert "recall" not in off and f"me{b}" not in off
        assert off == render_eye(store, h, "fisheye+1hop")  # the ring is untouched
        # the empty-ring hint must not advertise the rung the server won't run
        assert "+recall" not in render_eye(store, h, "fisheye+1hop")
        assert "recall" not in render_eye(store, h, "+recall")


# ── lexical / semantic ───────────────────────────────────────────────


def test_lexical_and_semantic_only_select_the_single_leg_in_ranking(
    store: Store,
) -> None:
    from precis.mesh_eval.retrieval import _search_arm

    emb = _SpyEmbedder(dim=store.embedding_dim())
    ref = store.insert_ref(kind="paper", slug="legs01", title="Leg fixture")
    question = "zyzzyva weevil taxonomy"
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            # lexical match only: carries the query words, unrelated vector
            ChunkInsert(
                ord=0,
                text="the zyzzyva weevil taxonomy was revised",
                embedding=MockEmbedder(dim=store.embedding_dim()).embed_one("other"),
            ),
            # semantic match only: no query words, vector == the query's
            ChunkInsert(
                ord=1,
                text="entirely different wording about beetles",
                embedding=MockEmbedder(dim=store.embedding_dim()).embed_one(question),
            ),
        ],
    )
    lex_handle = handle_registry.format_handle("paper", blocks[0].id, chunk=True)
    sem_handle = handle_registry.format_handle("paper", blocks[1].id, chunk=True)

    _out, hybrid = _search_arm(store, question, 10, emb)
    assert {lex_handle, sem_handle} <= set(hybrid)
    assert emb.calls == 1

    emb.calls = 0
    with applied(semantic=False):  # lexical-only
        _out, got = _search_arm(store, question, 10, emb)
    assert got == [lex_handle]
    assert emb.calls == 0  # the query was never embedded

    with applied(lexical=False):  # semantic-only
        _out, got = _search_arm(store, question, 10, emb)
    assert got[0] == sem_handle
    assert emb.calls == 1


def test_dispatch_rewrites_hybrid_search_mode_and_seeds_from_env(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    emb = _SpyEmbedder(dim=store.embedding_dim())
    rt = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store, embedder=emb))
    _seed_paper(store)
    plan = {"kind": "paper", "q": "chunk", "view": "plan"}

    assert "mode: hybrid" in rt.dispatch("search", dict(plan))
    monkeypatch.setenv(switches.ENV_VAR, "lexical-only")
    assert "mode: lexical" in rt.dispatch("search", dict(plan))
    monkeypatch.setenv(switches.ENV_VAR, "semantic-only")
    assert "mode: semantic" in rt.dispatch("search", dict(plan))
    # an explicit mode is the caller's call
    assert "mode: verbatim" in rt.dispatch("search", {**plan, "mode": "verbatim"})

    # lexical-only never embeds the query on a real (non-plan) search
    monkeypatch.setenv(switches.ENV_VAR, "lexical-only")
    emb.calls = 0
    rt.dispatch("search", {"kind": "paper", "q": "chunk"})
    assert emb.calls == 0
    monkeypatch.setenv(switches.ENV_VAR, "")
    rt.dispatch("search", {"kind": "paper", "q": "chunk"})
    assert emb.calls > 0

    # a bad env value is a rendered error, not a crash
    monkeypatch.setenv(switches.ENV_VAR, "lexical-only,semantic-only")
    out = rt.dispatch("search", dict(plan))
    assert "cannot be combined" in out and switches.ENV_VAR in out
    assert switches.current() == ALL_ON


def test_dispatch_seeds_extent_switches_from_env(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    text = "copper catalyses nitrate reduction"
    a = _embedded_memory(rt, text)
    _embedded_memory(rt, text)
    get = {"kind": "memory", "id": f"me{a}", "extent": "fisheye+1hop+recall"}
    assert "— recall" in rt.dispatch("get", get)
    monkeypatch.setenv(switches.ENV_VAR, "no-recall")
    assert "— recall" not in rt.dispatch("get", get)
    monkeypatch.setenv(switches.ENV_VAR, "no-fisheye")
    out = rt.dispatch("get", get)
    assert "— linked" not in out and "— recall" in out  # recall still on


# ── all-on is byte-identical ─────────────────────────────────────────


def test_all_on_is_byte_identical_to_no_switches(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    store = rt.hub.live_store
    ref_id = _seed_paper(store, slug="identical01")
    pa = handle_registry.format_handle("paper", ref_id)
    pc = _chunk_handle(store, ref_id, 3)
    mem = _embedded_memory(rt, "copper catalyses nitrate reduction")
    twin = _embedded_memory(rt, "copper catalyses nitrate reduction")
    store.add_link(src_ref_id=mem, dst_ref_id=twin, relation="related-to")
    store.add_tag(mem, Tag.closed("DOMAIN", "other"), set_by="system")

    def norm(text: str) -> str:
        # the head's age/read chip ticks (and the first dispatched read adds
        # its read count); neither is a switch effect
        return re.sub(r"\[created [^\]]*\]", "[created]", text)

    def surface() -> dict[str, str]:
        out: dict[str, str] = {}
        for h in (pa, pc, f"me{mem}"):
            for rung in (
                "kwd",
                "summary",
                "verbatim",
                "fisheye",
                "fisheye+1hop",
                "fisheye+2hop",
                "+recall",
            ):
                out[f"eye {h} {rung}"] = norm(render_eye(store, h, rung))
        calls = {
            "summaries": ("get", {"kind": "paper", "id": pa, "view": "summaries"}),
            "toc": ("get", {"kind": "paper", "id": pa, "view": "toc"}),
            "search": ("search", {"kind": "paper", "q": "chunk text"}),
            "lexical": ("search", {"kind": "paper", "q": "chunk", "mode": "lexical"}),
            "verbatim": ("search", {"kind": "paper", "q": "alpha", "mode": "verbatim"}),
            "plan": ("search", {"kind": "paper", "q": "chunk", "view": "plan"}),
            "mem-eye": (
                "get",
                {"kind": "memory", "id": f"me{mem}", "extent": "fisheye+1hop+recall"},
            ),
        }
        for name, (verb, args) in calls.items():
            out[name] = norm(rt.dispatch(verb, dict(args)))
        return out

    baseline = surface()
    assert "alpha" in baseline["toc"] or "alpha" in baseline["summaries"]
    with applied():  # explicit all-on
        assert surface() == baseline
    monkeypatch.setenv(switches.ENV_VAR, "")  # dispatch-seeded empty env
    assert surface() == baseline
    monkeypatch.setenv(switches.ENV_VAR, " , ")
    assert surface() == baseline
