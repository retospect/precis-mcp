"""Unit tests for the shared ref-mention grammar + resolver.

DB-free: ``resolve_link_targets`` is exercised against a hand-rolled
fake store (duck-typed ``fetch_refs_by_ids`` + ``pool.connection``) so
the write-time autolinker's resolution logic is covered without a live
postgres. The end-to-end "memory create writes links" path is covered
by the DB-backed tests in ``test_memory.py``.
"""

from __future__ import annotations

import re
from typing import Any

from precis.utils import mentions

# ---------------------------------------------------------------------------
# extract_handles
# ---------------------------------------------------------------------------


def test_extract_prefixed_bare_paper_and_conv() -> None:
    body = (
        "see memory:6134 and paper:acheson26~12 plus the thread "
        "discord/1490/151/999 — also futrell25 bare."
    )
    handles = mentions.extract_handles(body)
    assert ("memory", "6134", None) in handles
    assert ("paper", "acheson26", "~12") in handles
    assert ("conv", "discord/1490/151/999", None) in handles
    assert ("paper", "futrell25", None) in handles


def test_extract_dedups_and_strips_hash() -> None:
    # ``memory:#6134`` and ``memory:6134`` collapse; repeats dropped.
    handles = mentions.extract_handles("memory:#6134 memory:6134 memory:6134")
    assert handles == [("memory", "6134", None)]


def test_extract_gates_on_allowlist_and_low_signal() -> None:
    # ``user:`` is not a precis kind; ``tag:`` is low-signal. Neither
    # should surface, even though both match the ``noun:value`` shape.
    handles = mentions.extract_handles("user:asa tag:open memory:1")
    assert handles == [("memory", "1", None)]


def test_extract_handles_structure() -> None:
    """qu164903 dossier audit, slice A item 2: ``structure`` was
    missing from LINKIFY_KINDS (drifted from ``_REFS_BROWSABLE_KINDS`` in
    ``routes/refs.py``), so a bare ``structure:245406`` mention rendered
    literal instead of resolving."""
    assert "structure" in mentions.LINKIFY_KINDS
    handles = mentions.extract_handles("see structure:245406 for the geometry")
    assert ("structure", "245406", None) in handles


def test_extract_handles_pathway() -> None:
    """``pathway`` is browsable (``/refs/pathway/<id>``) and counts as
    computational evidence in the provenance classifier — it must linkify,
    same drift as ``structure``."""
    assert "pathway" in mentions.LINKIFY_KINDS
    handles = mentions.extract_handles("energetics in pathway:198000")
    assert ("pathway", "198000", None) in handles


def test_chunk_to_pos() -> None:
    assert mentions.chunk_to_pos("~12") == 12
    assert mentions.chunk_to_pos("~1..5") is None  # range, not one chunk
    assert mentions.chunk_to_pos("~p3") is None  # pdf page, not a chunk
    assert mentions.chunk_to_pos(None) is None


# ---------------------------------------------------------------------------
# resolve_link_targets — fake store
# ---------------------------------------------------------------------------


class _FakeRef:
    def __init__(
        self, ref_id: int, deleted_at: object = None, kind: str = "memory"
    ) -> None:
        self.id = ref_id
        self.deleted_at = deleted_at
        self.kind = kind


class _FakeStore:
    """Minimal store double: numeric id lookup + cite_key → ref_id, plus the
    patent DOCDB-slug regex path (``patent_slugs`` maps a stored patent
    ``cite_key`` / DOCDB slug → ref_id)."""

    def __init__(
        self,
        refs: dict[int, _FakeRef],
        cite_keys: dict[str, int],
        patent_slugs: dict[str, int] | None = None,
    ) -> None:
        self._refs = refs
        self._cite = cite_keys
        # A patent is addressed by its lowercased DOCDB slug (its cite_key).
        self._patent = {k.lower(): v for k, v in (patent_slugs or {}).items()}
        self.pool = self  # resolve_handle_ref does `store.pool.connection()`

    # -- cite_key lookup path ------------------------------------------
    def connection(self):
        store = self

        class _Ctx:
            def __enter__(self_):
                return store

            def __exit__(self_, *_a):
                return False

        return _Ctx()

    def execute(self, _sql: str, params: tuple):
        ident = params[0]
        # Route on the SQL: the patent path joins ``r.kind = 'patent'`` and
        # POSIX-regex-matches the DOCDB slug; the legacy path is cite_key.
        if "r.kind = 'patent'" in _sql:
            store_rid = None
            for slug in sorted(self._patent):  # deterministic: lowest slug
                rid = self._patent[slug]
                ref = self._refs.get(rid)
                if ref is None or getattr(ref, "deleted_at", None) is not None:
                    continue
                if getattr(ref, "kind", None) != "patent":
                    continue
                if re.match(str(ident), slug):
                    store_rid = rid
                    break
        else:
            store_rid = self._cite.get(ident)

        class _Cur:
            def fetchone(self_):
                return (store_rid,) if store_rid is not None else None

        return _Cur()

    # -- numeric lookup path -------------------------------------------
    def fetch_refs_by_ids(
        self, ids: list[int], include_deleted: bool = False
    ) -> dict[int, _FakeRef]:
        out: dict[int, _FakeRef] = {}
        for i in ids:
            ref = self._refs.get(i)
            if ref is None:
                continue
            if ref.deleted_at is not None and not include_deleted:
                continue
            out[i] = ref
        return out


def test_resolve_targets_numeric_slug_and_chunk() -> None:
    store = _FakeStore(
        refs={6134: _FakeRef(6134), 7: _FakeRef(7)},
        cite_keys={"acheson26": 7},
    )
    targets = mentions.resolve_link_targets(store, "memory:6134 and paper:acheson26~3")
    pairs = {(t.dst_ref_id, t.dst_pos) for t in targets}
    assert pairs == {(6134, None), (7, 3)}


def test_resolve_targets_agentlog_handle() -> None:
    """agentlog is on LINKIFY_KINDS so a dream memory can cite its tick's
    provenance node (``agentlog:<id>``) and auto-link back to it."""
    store = _FakeStore(refs={171286: _FakeRef(171286, kind="agentlog")}, cite_keys={})
    targets = mentions.resolve_link_targets(store, "I notice … (agentlog:171286)")
    assert {(t.dst_ref_id, t.dst_pos) for t in targets} == {(171286, None)}


def test_resolve_skips_missing_deleted_and_self() -> None:
    store = _FakeStore(
        refs={
            6134: _FakeRef(6134),
            50: _FakeRef(50, deleted_at="2026-01-01"),  # soft-deleted
        },
        cite_keys={},
    )
    # 9999 missing, 50 deleted, 6134 == exclude → all dropped.
    targets = mentions.resolve_link_targets(
        store,
        "memory:6134 memory:9999 memory:50",
        exclude_ref_id=6134,
    )
    assert targets == []


def test_resolve_dedups_repeated_target() -> None:
    store = _FakeStore(refs={6134: _FakeRef(6134)}, cite_keys={})
    targets = mentions.resolve_link_targets(store, "memory:6134 memory:6134")
    assert [(t.dst_ref_id, t.dst_pos) for t in targets] == [(6134, None)]


# ---------------------------------------------------------------------------
# Patent public-number autolinking (gripe #48807)
# ---------------------------------------------------------------------------


def test_bracketed_patent_pubnum_links_case_insensitively() -> None:
    # Memory cites the public number lower-cased; the stored DOCDB slug is
    # matched case-insensitively.
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="patent")},
        cite_keys={},
        patent_slugs={"us9927397b1": 70},
    )
    targets = mentions.resolve_link_targets(store, "see [US9927397B1] for the method")
    assert [(t.dst_ref_id, t.dst_pos) for t in targets] == [(70, None)]


def test_bracketed_pubnum_without_kind_code_matches_slug() -> None:
    # A citation writes the bare number (no kind code); the stored slug
    # carries the grant suffix. ``[US2943737]`` → ``us2943737a``.
    store = _FakeStore(
        refs={71: _FakeRef(71, kind="patent")},
        cite_keys={},
        patent_slugs={"us2943737a": 71},
    )
    targets = mentions.resolve_link_targets(store, "as in [US2943737], the oil…")
    assert [(t.dst_ref_id, t.dst_pos) for t in targets] == [(71, None)]


def test_bracketed_pubnum_dedups_across_case() -> None:
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="patent")},
        cite_keys={},
        patent_slugs={"us9927397b1": 70},
    )
    # Two mentions in different case resolve to the same patent → one edge.
    targets = mentions.resolve_link_targets(
        store, "[us9927397b1] and again [US9927397B1]"
    )
    assert [(t.dst_ref_id, t.dst_pos) for t in targets] == [(70, None)]


def test_unknown_bracketed_pubnum_stays_literal() -> None:
    # No patent row → no link (the over-fire gate). Prose like [US0000]
    # must not spuriously link.
    store = _FakeStore(refs={}, cite_keys={}, patent_slugs={})
    assert mentions.resolve_link_targets(store, "the [US0000] filing") == []


def test_bracketed_pubnum_on_non_patent_kind_is_dropped() -> None:
    # A same-shaped slug belonging to another kind must not masquerade
    # as a patent link (the SQL join filters r.kind = 'patent').
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="finding")},
        cite_keys={},
        patent_slugs={"us9927397b1": 70},
    )
    assert mentions.resolve_link_targets(store, "[us9927397b1]") == []


def test_unbracketed_pubnum_never_links() -> None:
    # Only the bracketed form is a link intent; bare prose stays literal
    # even when the patent exists (avoids over-firing on `US1234` text).
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="patent")},
        cite_keys={},
        patent_slugs={"us9927397b1": 70},
    )
    assert mentions.resolve_link_targets(store, "US9927397B1 was granted") == []


# ---------------------------------------------------------------------------
# BARE_BRACKET_REF_PATTERN — authorial pin (Taproot slice A2, Phase 2):
# `[fi<id>>pa5,pc9]` (replace) / `[fi<id>+pa5]` (supplement). Additive
# optional group — `bare` is captured byte-identically with or without a
# pin, so every existing consumer that reads only `m.group("bare")` is
# unaffected by a pin's presence.
# ---------------------------------------------------------------------------


def test_strip_page_anchor_links_keeps_bracketed_label() -> None:
    # Marker's inert PDF page-anchor citation → plain bracketed citation.
    assert (
        mentions.strip_page_anchor_links("our previous Letter [11](#page-5-0).")
        == "our previous Letter [11]."
    )
    # Single-number anchor form (#page-N) too.
    assert mentions.strip_page_anchor_links("see [3](#page-2)") == "see [3]"


def test_strip_page_anchor_links_normalises_inner_whitespace() -> None:
    # A blank line Marker's block-merge fused inside the bracket span must be
    # collapsed — else the claim-page paragraph splitter shreds it into a
    # stray <p>11</p>.
    assert (
        mentions.strip_page_anchor_links("Letter [\n\n11\n\n](#page-5-0). Next")
        == "Letter [11]. Next"
    )


def test_strip_page_anchor_links_drops_empty_anchor() -> None:
    assert mentions.strip_page_anchor_links("x [](#page-5-0) y") == "x  y"


def test_strip_page_anchor_links_leaves_real_external_links() -> None:
    # Scoped to the #page-N-M href shape — a legitimate external link survives.
    src = "see [Nature](https://doi.org/10.1/abc) for more"
    assert mentions.strip_page_anchor_links(src) == src


def test_strip_page_anchor_links_is_idempotent() -> None:
    once = mentions.strip_page_anchor_links("Letter [11](#page-5-0).")
    assert mentions.strip_page_anchor_links(once) == once


def test_bare_bracket_pattern_no_pin_unchanged() -> None:
    m = mentions.BARE_BRACKET_REF_PATTERN.fullmatch("[fi42]")
    assert m is not None
    assert m.group("bare") == "fi42"
    assert m.group("pin") is None


def test_bare_bracket_pattern_replace_pin() -> None:
    m = mentions.BARE_BRACKET_REF_PATTERN.fullmatch("[fi42>pa5,pc9]")
    assert m is not None
    assert m.group("bare") == "fi42"
    assert m.group("pin") == ">pa5,pc9"


def test_bare_bracket_pattern_supplement_pin() -> None:
    m = mentions.BARE_BRACKET_REF_PATTERN.fullmatch("[fi42+pa5]")
    assert m is not None
    assert m.group("bare") == "fi42"
    assert m.group("pin") == "+pa5"


def test_bare_bracket_pattern_non_finding_handles_have_no_pin() -> None:
    for token in ("[me5]", "[pc10]", "[dc41]"):
        m = mentions.BARE_BRACKET_REF_PATTERN.fullmatch(token)
        assert m is not None, token
        assert m.group("bare") == token[1:-1]
        assert m.group("pin") is None


def test_bare_bracket_pattern_sigil_forms_never_carry_a_pin() -> None:
    # The `[¶§][^\[\]]+` alternative is greedy and consumes to the closing
    # `]`, so a pin group can never attach to a sigil form — pins are
    # finding-only. `[§foo~1]` still parses exactly as before.
    m = mentions.BARE_BRACKET_REF_PATTERN.fullmatch("[§foo~1]")
    assert m is not None
    assert m.group("bare") == "§foo~1"
    assert m.group("pin") is None


def test_parse_pin_suffix() -> None:
    assert mentions.parse_pin_suffix(None) == (None, [])
    assert mentions.parse_pin_suffix(">pa5,pc9") == (">", ["pa5", "pc9"])
    assert mentions.parse_pin_suffix("+pa5") == ("+", ["pa5"])


def test_autolink_invariance_pin_ignored_for_link_targets(store: Any) -> None:
    """A pin is a draft-export directive, not a link-graph edge — a pinned
    finding handle must materialise the SAME autolink target as the bare
    handle (:func:`resolve_link_targets`, which reads only `bare`), never a
    broken/missing one."""
    from precis.utils import handle_registry

    ref = store.insert_ref(kind="finding", slug=None, title="t", meta={})
    handle = handle_registry.format_handle("finding", ref.id)

    plain = mentions.resolve_link_targets(store, f"see [{handle}]")
    pinned = mentions.resolve_link_targets(store, f"see [{handle}>pa5]")

    assert [(t.dst_ref_id, t.dst_pos) for t in plain] == [(ref.id, None)]
    assert [(t.dst_ref_id, t.dst_pos) for t in pinned] == [(ref.id, None)]
