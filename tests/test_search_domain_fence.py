"""gr572720: the domain fence on the default cross-kind fan-out.

A connection bound to a named domain (``request_domain`` /
``PRECIS_DOMAIN`` / the per-call ``args={'domain': …}`` override) gets
its *default* kind expansion — an omitted ``kind=`` or a wildcard
alias — intersected with the domain's ``kinds``. An explicit ``kind=``
or comma list outside the set still runs; ``'all'`` is unrestricted,
byte-identical to the unfenced fan-out. The footer names what was
skipped and the door.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import precis.runtime.scope as scope_mod
from precis.embedder import MockEmbedder
from precis.runtime.core import PrecisRuntime
from precis.runtime.scope import DOMAINS, Domain, request_domain
from precis.store import ChunkInsert, Store
from precis.utils.search_merge import SearchHit


@pytest.fixture(autouse=True)
def _clean_domain_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient PRECIS_DOMAIN or bound request domain leaks in."""
    monkeypatch.delenv(scope_mod.DOMAIN_ENV, raising=False)
    scope_mod._request_domain.set(None)


def _hit(kind: str, title: str, ref_id: int) -> SearchHit:
    return SearchHit(
        score=0.5, kind=kind, title=title, preview="p", ref_id=ref_id, slug=None
    )


def _record_fan_out(
    monkeypatch: pytest.MonkeyPatch, rt: PrecisRuntime
) -> dict[str, list[str]]:
    """Stub the per-kind dispatch; record the kind list it was handed."""
    seen: dict[str, list[str]] = {}

    def fake(
        self: PrecisRuntime,
        kinds: list[str],
        kw: dict,
        kind_overrides: dict | None = None,
        *,
        deadline_at: float | None = None,
    ) -> dict[str, object]:
        seen["kinds"] = list(kinds)
        return {k: [_hit(k, f"{k} hit", i + 1)] for i, k in enumerate(kinds)}

    monkeypatch.setattr(type(rt), "_cross_kind_fan_out", fake)
    return seen


def test_pcb_domain_fan_out_never_dispatches_outside_kinds(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pcb fan-out stays inside the domain: se/chem/protein never
    dispatch, and the footer names the skip + the door."""
    rt = runtime_with_store
    seen = _record_fan_out(monkeypatch, rt)
    domain = DOMAINS["pcb"]
    assert domain.kinds is not None
    with request_domain(domain):
        resp = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    dispatched = seen["kinds"]
    assert dispatched, "the pcb domain still leaves searchable kinds"
    assert set(dispatched) <= domain.kinds
    for outside in ("se", "chem", "protein"):
        assert outside not in dispatched
    # The fence actually bit: some search_hits-eligible kinds sit
    # outside pcb (``se`` at minimum — it opts into the merge).
    eligible = set(rt._cross_kind_kinds())
    skipped = eligible - domain.kinds
    assert skipped, "test build has no eligible kinds outside pcb"
    assert f"{len(skipped)} kinds outside domain pcb not searched" in resp.body
    assert 'args={"domain": "all"}' in resp.body
    assert (
        f"kind='{','.join(k for k in rt._cross_kind_kinds() if k in skipped)}'"
        in resp.body
    )


def test_pcb_domain_env_overlay_fences_the_same(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_DOMAIN=pcb`` resolves to the registry entry — same fence
    as a request binding."""
    rt = runtime_with_store
    seen = _record_fan_out(monkeypatch, rt)
    monkeypatch.setenv("PRECIS_DOMAIN", "pcb")
    rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    assert set(seen["kinds"]) <= (DOMAINS["pcb"].kinds or set())


def test_domain_all_is_byte_identical_to_unfenced(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``'all'`` leaves the kinds axis open: no skip footer, identical
    body — by env, by request binding, and by the call-level override
    the footer advertises (including under a bound pcb domain)."""
    rt = runtime_with_store
    seen = _record_fan_out(monkeypatch, rt)
    baseline = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    unfenced = seen["kinds"]

    with request_domain(DOMAINS["all"]):
        bound = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    assert bound.body == baseline.body
    assert seen["kinds"] == unfenced

    monkeypatch.setenv("PRECIS_DOMAIN", "all")
    env = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    assert env.body == baseline.body

    # The door itself: a bound pcb connection widens one call to 'all',
    # and the nested ``args={}`` spelling hoists to the same override.
    monkeypatch.setenv("PRECIS_DOMAIN", "pcb")
    via_arg = rt._dispatch_cross_kind(
        "*", {"q": "dogfood", "mode": "lexical", "__extras__": {"domain": "all"}}
    )
    assert via_arg.body == baseline.body
    assert seen["kinds"] == unfenced


def test_explicit_kind_and_comma_list_outside_domain_still_run(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit kind= or comma list is caller intent, not the
    default expansion — the fence leaves it alone."""
    rt = runtime_with_store
    seen = _record_fan_out(monkeypatch, rt)
    with request_domain(DOMAINS["pcb"]):
        rt._dispatch_cross_kind("se", {"q": "dogfood", "mode": "lexical"})
        assert seen["kinds"] == ["se"]
        rt._dispatch_cross_kind("se,draft", {"q": "dogfood", "mode": "lexical"})
        assert seen["kinds"] == ["se", "draft"]


def test_explicit_outside_kind_answers_through_dispatch(
    runtime_with_store: PrecisRuntime,
) -> None:
    """kind='se'/'chem' outside pcb: the search verb still reaches the
    kind's own handler — the fence never touches a named kind."""
    rt = runtime_with_store
    with request_domain(DOMAINS["pcb"]):
        body, is_error = rt.dispatch_with_status(
            "search", {"kind": "se", "q": "zzz-no-such-design"}
        )
        assert not is_error
        assert "outside domain" not in body


def test_unknown_domain_arg_refuses_loud(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``args={'domain': 'bogus'}`` refuses with the registry as the
    option list — a typo never silently widens to 'all'."""
    from precis.errors import BadInput

    rt = runtime_with_store
    with pytest.raises(BadInput, match="bogus"):
        rt._dispatch_cross_kind(
            "*",
            {"q": "dogfood", "mode": "lexical", "__extras__": {"domain": "bogus"}},
        )


def test_tags_only_sweep_fences_to_the_domain(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The queryless tags sweep fans out to the domain's kinds too —
    the empty body names only the fenced set, plus the same footer."""
    rt = runtime_with_store
    domain = DOMAINS["pcb"]
    assert domain.kinds is not None
    with request_domain(domain):
        resp = rt._dispatch_cross_kind("*", {"tags": ["topic-domain-fence"]})
    assert "kinds outside domain pcb not searched" in resp.body
    searched = resp.body.split("across [")[1].split("]")[0].split(", ")
    assert "'se'" not in searched
    assert all(k.strip("'") in domain.kinds for k in searched)


def test_ad_hoc_domain_fences_without_registry(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request-bound Domain need not be registered (the token seam
    binds constructed values) — the fence reads its kinds axis."""
    rt = runtime_with_store
    seen = _record_fan_out(monkeypatch, rt)
    narrow = Domain(name="narrow", kinds=frozenset({"memory", "todo"}))
    with request_domain(narrow):
        rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    assert seen["kinds"] == [
        k for k in rt._cross_kind_kinds() if k in {"memory", "todo"}
    ]


def test_source_search_budget_fit_keeps_the_fence_footer(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source-search ``budget_fit`` rebuilds an over-cap page as
    head + cut rows + a resume tail — the fence footer is a post-split
    append, not a hit row, so it must ride that tail or a fitted page
    drops the skipped-kinds notice and the door (td611330 review)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    marker = f"zephyr{uuid4().hex[:8]}"
    for i in range(30):
        ref = store.insert_ref(
            kind="memory", slug=None, title=f"{marker} note {i}", meta={}
        )
        text = f"{marker} tide gauge reading {i}"
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=text, embedding=emb.embed_one(text))],
        )
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "1200")
    with request_domain(DOMAINS["pcb"]):
        body, is_error = runtime_with_store.dispatch_with_status(
            "search",
            {
                "kind": "*",
                "q": marker,
                "mode": "lexical",
                "since": "2000-01-01",
                "page_size": 30,
            },
        )
    assert is_error is False, body
    # The fit really ran — the assertions below are not the unfitted body.
    assert "… truncated: showing" in body
    skipped = set(runtime_with_store._cross_kind_kinds()) - (
        DOMAINS["pcb"].kinds or set()
    )
    assert f"{len(skipped)} kinds outside domain pcb not searched" in body
    assert 'args={"domain": "all"}' in body
    # …and it sits past the resume call, where the unfitted page puts it.
    assert body.index("kinds outside domain pcb") > body.index("next: search(")
