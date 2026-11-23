"""The per-chunk ⚠ issue list (``smartdraft.chunk_issues``) and its
retraction-cite source (``smartdraft.retraction_issues_in``, gr738927)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from precis_web import smartdraft

#: td767081: a status with no recorded notice (every paper here: the fake
#: store holds no notices) cannot be judged or closed, so its label says to
#: run a forced check (``_retraction_marks.LEGACY_HINT``).
_LEGACY = " (no notice recorded: run a forced retraction check)"


def test_clean_chunk_has_no_issues() -> None:
    assert smartdraft.chunk_issues(True, None, []) == []


def test_issues_collect_from_every_source() -> None:
    trust = {
        "label": "unsupported",
        "heads": [
            {"head": "fi1", "label": "unsupported", "note": ""},
            {"head": "fi2", "label": "abstract", "note": "x"},
        ],
    }
    citations = [
        {"kind": "retracted-citation", "text": "cited paper [pa1] is retracted"}
    ]
    kinds = [i["kind"] for i in smartdraft.chunk_issues(False, trust, citations)]
    assert kinds == ["unbacked-claim", "retracted-citation", "citation-integrity"]


class _RefStore:
    """``get_ref``/``resolve_handle`` over canned tables; counts both so
    the rendered window's one-lookup cost bar is assertable.

    ``resolves`` maps a handle token to a ``ResolvedHandle``-shaped
    namespace — ``resolve_cite_handle`` reads ``public_id`` (the cite key
    a cite resolves to), ``cite_public_id`` (the SI parent's key, set
    only for a supplement record) and ``chunk_id``; ``slugs`` maps a cite
    key/slug to the ref_id ``get_ref`` returns."""

    def __init__(self, refs: dict[int, Any]) -> None:
        self._refs = refs
        self.gets: list[Any] = []
        self.handles: list[str] = []
        self.resolves: dict[str, Any] = {}
        self.slugs: dict[str, int] = {}

    def get_ref(self, *, kind: str, id: Any, include_deleted: bool = False) -> Any:
        self.gets.append(id)
        rid: Any = self.slugs.get(id) if isinstance(id, str) else id
        ref = self._refs.get(rid)
        if ref is None:
            return None
        return SimpleNamespace(
            **vars(ref), id=rid, slug=id if isinstance(id, str) else None
        )

    def resolve_handle(self, handle: str) -> Any:
        self.handles.append(handle)
        return self.resolves.get(handle)


def _resolved(
    public_id: str,
    *,
    cite_public_id: str | None = None,
    chunk_id: int | None = None,
) -> Any:
    """A ``ResolvedHandle`` stand-in — the three attributes
    ``resolve_cite_handle`` reads."""
    return SimpleNamespace(
        public_id=public_id,
        cite_public_id=cite_public_id,
        chunk_id=chunk_id,
    )


def _paper(retraction_status: str | None = None, checked: bool = True) -> Any:
    return SimpleNamespace(
        retraction_status=retraction_status,
        retraction_checked_at=datetime.now(UTC) if checked else None,
        kind="paper",
        pdf_role=None,
    )


def test_retraction_issues_in_flags_statused_and_never_checked() -> None:
    store = _RefStore(
        {
            10: _paper("retracted"),
            11: _paper(checked=False),
            12: _paper(),
        }
    )
    store.resolves = {
        "pa10": _resolved("s10"),
        "pa11": _resolved("s11"),
        "pa12": _resolved("s12"),
    }
    store.slugs = {"s10": 10, "s11": 11, "s12": 12}
    issues = smartdraft.retraction_issues_in(
        store, "Cites [pa10], [pa11] and [pa12].", {}
    )
    assert issues == [
        {
            "kind": "retracted-citation",
            "text": "cited paper [pa10] is retracted" + _LEGACY,
        },
        {
            "kind": "unchecked-citation",
            "text": "cited paper [pa11] was never checked for retraction",
        },
    ]


def test_retraction_issues_in_never_checked_is_first_class() -> None:
    """``retraction_checked_at IS NULL`` earns a triangle even with no
    status — the pane's ``unchecked`` bucket's per-chunk twin (gr725269)."""
    store = _RefStore({10: _paper(checked=False)})
    store.resolves["pa10"] = _resolved("s10")
    store.slugs = {"s10": 10}
    issues = smartdraft.retraction_issues_in(store, "See [pa10].", {})
    assert [i["kind"] for i in issues] == ["unchecked-citation"]


def test_retraction_issues_in_ignores_dead_and_foreign_cites() -> None:
    store = _RefStore({10: _paper("corrected")})
    # [pa99] resolves to nothing (cite_integrity_ok's flag, not ours) and
    # [fi42] is a finding handle — a claim hub's trust is
    # claim_trust_for_block's lane, not this citation walk's.
    assert smartdraft.retraction_issues_in(store, "Cites [pa99] and [fi42].", {}) == []


def test_retraction_issues_in_resolves_chunk_handles() -> None:
    store = _RefStore({10: _paper("expression_of_concern")})
    store.resolves["pc7"] = _resolved("s10", chunk_id=7)
    store.slugs = {"s10": 10}
    issues = smartdraft.retraction_issues_in(store, "Cites [pc7].", {})
    assert issues == [
        {
            "kind": "retracted-citation",
            "text": "cited paper [pc7] is expression of concern" + _LEGACY,
        }
    ]


def test_retraction_issues_in_flags_bare_handle_cites() -> None:
    """A bare ``pa<id>`` ≥4 digits cites like the bracketed form — the
    export grammar's ``bcite`` arm (``BARE_CITABLE_PATTERN``)."""
    store = _RefStore({10: _paper("retracted")})
    store.resolves["pa12345"] = _resolved("s10")
    store.slugs = {"s10": 10}
    issues = smartdraft.retraction_issues_in(store, "See pa12345 for the details.", {})
    assert issues == [
        {
            "kind": "retracted-citation",
            "text": "cited paper [pa12345] is retracted" + _LEGACY,
        }
    ]
    # Under the 4-digit floor a bare token stays prose (``pt111`` a
    # surface, ``co2`` a molecule) — the export keeps it literal too.
    short = _RefStore({5: _paper("retracted")})
    short.resolves["pa5"] = _resolved("s5")
    short.slugs = {"s5": 5}
    assert smartdraft.retraction_issues_in(short, "See pa5 here.", {}) == []


def test_retraction_issues_in_flags_display_link_cites() -> None:
    """``[source](pa12345)`` — the display-link arm's handle target cites
    the paper; a non-cite target (``me5``, a URL) never reaches here."""
    store = _RefStore({10: _paper("retracted"), 11: _paper(checked=False)})
    store.resolves["pa12345"] = _resolved("s10")
    store.slugs = {"s10": 10}
    issues = smartdraft.retraction_issues_in(
        store,
        "As [the source](pa12345) shows — unlike [a note](me5) "
        "or [the web](https://example.com).",
        {},
    )
    assert issues == [
        {
            "kind": "retracted-citation",
            "text": "cited paper [pa12345] is retracted" + _LEGACY,
        }
    ]


def test_retraction_issues_in_follows_merge_redirects() -> None:
    """``[pa<id>]`` naming a merged-away record resolves to the SURVIVOR:
    ``resolve_cite_handle`` returns the survivor's cite key, where a
    raw-pk ``get_ref`` reads the tombstoned id and misses the retracted
    survivor entirely."""
    store = _RefStore({20: _paper("retracted")})
    store.resolves["pa99"] = _resolved("surv20")  # pa99 merged into ref 20
    store.slugs = {"surv20": 20}
    issues = smartdraft.retraction_issues_in(store, "Cites [pa99].", {})
    assert issues == [
        {
            "kind": "retracted-citation",
            "text": "cited paper [pa99] is retracted" + _LEGACY,
        }
    ]


def test_retraction_issues_in_cites_si_parent() -> None:
    """A cite into a supplement record counts the PARENT paper's state —
    ``ResolvedHandle.cite_public_id``, the same hop the export's
    ``_handle_cite`` / ``cite_target_for`` make."""
    store = _RefStore({30: _paper("retracted")})
    store.resolves["pa31"] = _resolved("si31", cite_public_id="parent30")
    store.slugs = {"parent30": 30}
    issues = smartdraft.retraction_issues_in(store, "See [pa31].", {})
    assert issues == [
        {
            "kind": "retracted-citation",
            "text": "cited paper [pa31] is retracted" + _LEGACY,
        }
    ]


def test_retraction_issues_in_flags_handle_with_selector() -> None:
    """``[pa5~3]`` still cites the record — the ``~n`` selector is
    display detail the resolver's base-fallback drops."""
    store = _RefStore({5: _paper(checked=False)})
    store.resolves["pa5"] = _resolved("s5")
    store.slugs = {"s5": 5}
    issues = smartdraft.retraction_issues_in(store, "See [pa5~3].", {})
    assert issues == [
        {
            "kind": "unchecked-citation",
            "text": "cited paper [pa5] was never checked for retraction",
        }
    ]


def test_retraction_issues_in_flags_pasted_rawcite_line() -> None:
    """A pasted ``pa<id> <citation text>`` line at line start — the
    LaTeX grammar's ``rawcite`` arm — resolves its leading handle."""
    store = _RefStore({10: _paper("retracted")})
    store.resolves["pa12345"] = _resolved("s10")
    store.slugs = {"s10": 10}
    issues = smartdraft.retraction_issues_in(
        store, "pa12345 Smith et al., Journal of Tests (2020).", {}
    )
    assert [i["kind"] for i in issues] == ["retracted-citation"]


def test_retraction_issues_in_resolves_slug_cite_forms() -> None:
    """``[§slug~n]``, ``paper:slug~n`` and a bare key resolve like the
    export, so the triangle matches the collated retraction report."""
    store = _RefStore({20: _paper("retracted"), 21: _paper(checked=False)})
    store.slugs = {"smith24": 20, "jones23": 21}
    for text, label in [
        ("Shown [§smith24~3].", "smith24"),
        ("Shown paper:smith24~3.", "smith24"),
        ("Shown smith24.", "smith24"),
    ]:
        issues = smartdraft.retraction_issues_in(store, text, {})
        assert issues == [
            {
                "kind": "retracted-citation",
                "text": f"cited paper [{label}] is retracted" + _LEGACY,
            }
        ], text
    issues = smartdraft.retraction_issues_in(store, "See [§jones23~p2].", {})
    assert [i["kind"] for i in issues] == ["unchecked-citation"]
    assert smartdraft.retraction_issues_in(store, "See [§nobody99].", {}) == []


def test_retraction_issues_in_caches_resolution_across_the_window() -> None:
    """One lookup per distinct token/key across the WHOLE render — the
    documented per-render cost bar ``cite_integrity_ok``'s cache sets.
    Driven through the same ``RefLookupMemo`` ``review_payloads_for``
    wraps the store in, so ``get_ref``/``resolve_handle`` themselves are
    once-per-window."""
    from precis.export._ref_cache import RefLookupMemo

    store = _RefStore({20: _paper(checked=False)})
    store.resolves["pa8"] = _resolved("s20")
    store.slugs = {"s20": 20, "smith24": 20}
    mstore = RefLookupMemo(store)
    cache: dict[Any, Any] = {}
    smartdraft.retraction_issues_in(mstore, "See [pa8].", cache)
    smartdraft.retraction_issues_in(mstore, "Also [pa8] and smith24.", cache)
    smartdraft.retraction_issues_in(mstore, "smith24 once more.", cache)
    assert store.handles == ["pa8"]
    assert store.gets == ["s20", "smith24"]


def test_retraction_issues_in_skips_bare_handles_in_code_and_math() -> None:
    """A bare ``pa<id>`` inside inline code or plausible math is literal
    content — ``_iter_refs``'s verbatim-span guard, the same one the
    export's ``_render_inline`` runs (td607394). The bracketed arm stays
    live inside code, matching the export and the reader."""
    store = _RefStore({10: _paper("retracted")})
    store.resolves["pa12345"] = _resolved("s10")
    store.slugs = {"s10": 10}
    for text in [
        "Use `pa12345` for the handle form.",
        "See $x = pa12345^2$ here.",
        "See $$pa12345$$ here.",
    ]:
        assert smartdraft.retraction_issues_in(store, text, {}) == [], text
    # Outside a verbatim span the same token still flags: prose beside a
    # code span, and inside an implausible ``$…$`` pairing the math gate
    # rejects (``$10 and pa12345 $`` — two currency dollars, no math).
    expected = [
        {
            "kind": "retracted-citation",
            "text": "cited paper [pa12345] is retracted" + _LEGACY,
        }
    ]
    for text in [
        "Code `notes` then pa12345 for details.",
        "Costs $10 and pa12345 $20 here.",
        # A bracketed cite inside code still flags — only the
        # bcite/rawcite arms are prose-gated, same as the export.
        "`[pa12345]`",
    ]:
        assert smartdraft.retraction_issues_in(store, text, {}) == expected, text


def test_retraction_issues_in_skips_rawcite_inside_code_span() -> None:
    """A pasted citation line inside a multi-line inline code span is
    code, not a cite — the ``rawcite`` arm gets ``_iter_refs``'s same
    verbatim-span guard."""
    store = _RefStore({10: _paper("retracted")})
    store.resolves["pa12345"] = _resolved("s10")
    store.slugs = {"s10": 10}
    text = "`code notes:\npa12345 Smith et al., Journal of Tests (2020).\nend`"
    assert smartdraft.retraction_issues_in(store, text, {}) == []
