"""Fixtures for the precis_web route tests.

The routes read structured data off a ``Store`` and route writes
through ``runtime.dispatch_with_status``. These fakes implement just
enough of both surfaces to exercise every route without a Postgres
connection: ``list_refs`` / ``search_refs_lexical`` / ``fetch_refs_by_ids``
return canned refs, and the fake pool's cursor returns empty result
sets (so the tag-join / status SQL degrades cleanly — exactly the
defensive path the ``_safe`` wrapper and tag defaults are built for).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig


def make_ref(**kw: Any) -> SimpleNamespace:
    """A duck-typed ``Ref`` carrying the attrs the routes read."""
    base = {
        "id": 1,
        "kind": "todo",
        "slug": None,
        "title": "untitled",
        "year": None,
        "parent_id": None,
        "pdf_sha256": None,
        "authors": None,
        "updated_at": None,
        "meta": {},
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        return _FakeCursor([])


class _FakePool:
    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        yield _FakeConn()


class FakeStore:
    def __init__(self) -> None:
        self.pool = _FakePool()
        #: ref_ids the fake reports as carrying OPEN:needs-triage (tests
        #: populate this to exercise the triage panel / tag-clear paths).
        self.triaged_ref_ids: set[int] = set()
        #: {ref_id: {open-value, …}} the fake reports via ``ref_tag_values``
        #: (the batched flag-state probe for a list view — tests populate
        #: it to render active read-later / must-read / skim buttons).
        self.ref_open_values: dict[int, set[str]] = {}
        #: ref_ids soft-deleted via the web delete route (the route calls
        #: the store directly — paper delete is web-only, not dispatched).
        self.deleted_ref_ids: set[int] = set()
        #: (victim, survivor) pairs merged via the resolve-duplicate route.
        self.merges: list[tuple[int, int]] = []
        #: ref_ids stamped via touch_viewed (the reader page-open access
        #: stamp that drives the drafts most-recently-opened order).
        self.viewed: list[int] = []
        #: (ref_id, meta-updates) tuples written via stamp_ref_meta — the
        #: genre/brief workspace writes from the /drafts/<id>/workspace route.
        self.meta_writes: list[tuple[int, dict[str, Any]]] = []
        #: (ref_id, scheme, value) tuples written via set_ref_identifier
        #: (the slug-rename path), plus cite_keys to report as taken so the
        #: collision branch can be exercised.
        self.identifier_writes: list[tuple[int, str, str]] = []
        self.taken_cite_keys: set[str] = set()
        #: Corpus-presence ledger fakes. ``missing_pdf_shas`` are the shas
        #: ``pdf_missing`` reports as held-but-missing (empty → nothing
        #: flagged); ``storage_paths`` seeds ``pdf_storage_path``;
        #: ``storage_path_writes`` records ``set_pdf_storage_path`` calls.
        self.missing_pdf_shas: set[str] = set()
        self.storage_paths: dict[str, str] = {}
        self.storage_path_writes: list[tuple[str, str]] = []
        #: Canned sidebar-nav hits per scope_ref_id: lists of
        #: (block, ref, score) for the search_blocks_* fakes.
        self.nav_hits: dict[int, list[Any]] = {}
        #: ref_ids passed to bump_salience_for_ref — the reader heats a
        #: document on open (summarize hot tier + dreamer signal).
        self.salience_bumps: list[int] = []
        #: Canned chunk-handle table for resolve_handle: chunk_id ->
        #: (ref_id, ord, kind). Tests populate it to exercise the
        #: console resolver's chunk-handle branch (``pc…``).
        self.chunk_handles: dict[int, tuple[int, int, str]] = {}
        self.todos = [
            make_ref(id=1, kind="todo", title="Build the thing", parent_id=None),
            make_ref(id=2, kind="todo", title="Draft the spec", parent_id=1),
        ]
        self.papers = [
            make_ref(
                id=10,
                kind="paper",
                slug="smith2024",
                title="A paper",
                year=2024,
                pdf_sha256="abc",
                authors=[{"family": "Smith", "given": "Jane"}],
                meta={"abstract": "<jats:p>We study <b>X</b> in depth.</jats:p>"},
            ),
            make_ref(
                id=11,
                kind="paper",
                slug="jones2025",
                title="Another paper",
                year=2025,
                pdf_sha256="def",
                authors=[{"family": "Jones", "given": "Bob"}],
                meta={},  # no publisher abstract -> backfilled from chunks
            ),
            # Stubs for the Papers-Needed tab — pdf_sha256 None.
            make_ref(
                id=90,
                kind="paper",
                slug="javey2003",
                title="Ballistic carbon nanotube field-effect transistors",
                year=2003,
                pdf_sha256=None,
                authors=[],
                meta={},
            ),
            make_ref(
                id=91,
                kind="paper",
                slug="novoselov2004",
                title="",
                year=None,
                pdf_sha256=None,
                authors=[],
                meta={},
            ),
        ]
        self.memories = [
            make_ref(id=20, kind="memory", title="A decision"),
            make_ref(id=21, kind="memory", title="An idea"),
        ]
        # A failed plan_tick job under an LLM-planner todo (id=81), plus a
        # legacy orphan job (no parent) — the two branches of the job
        # detail actions strip.
        self.todos.append(
            make_ref(id=81, kind="todo", title="Plan the campaign", parent_id=None)
        )
        self.jobs = [
            make_ref(
                id=80,
                kind="job",
                title="plan_tick (dispatched from todo:81)",
                parent_id=81,
                meta={
                    "job_type": "plan_tick",
                    "executor": "claude_inproc",
                    "transcript": '{"type":"assistant"}',
                },
            ),
            make_ref(
                id=82,
                kind="job",
                title="orphan job",
                parent_id=None,
                meta={"job_type": "fix_gripe"},
            ),
        ]
        self.oracles = [
            make_ref(id=30, kind="oracle", slug="planck-constant", title="Planck"),
        ]
        # Web ref to exercise the expanded-allowlist detail path —
        # T12.6 expanded ``_REFS_BROWSABLE_KINDS`` to 19+ kinds but
        # the legacy ``_REF_KIND_LABEL`` only had 6, so /refs/web/N
        # KeyError'd on the lookup. Carry one fixture so the
        # regression test can hit the route.
        self.webs = [
            make_ref(
                id=70,
                kind="web",
                slug="example.com/page",
                title="A cached web page",
            ),
        ]
        # A pres slide deck for the /pres reader/editor render test.
        self.press = [
            make_ref(
                id=60,
                kind="pres",
                slug="2001-lecture01",
                title="lecture01",
                pdf_sha256="pdeck",
                meta={"authors": ["Payne, M. C."], "venue": "CASTEP Workshop"},
            ),
        ]
        # A datasheet for the /datasheets reader render test — carries the
        # vendor / sub-type / part meta the datasheet Meta panel edits.
        self.datasheets = [
            make_ref(
                id=95,
                kind="datasheet",
                slug="esp32c3",
                title="ESP32-C3 Datasheet",
                pdf_sha256="dsheet",
                meta={
                    "vendor": "Espressif Systems",
                    "subtype": "app-note",
                    "part_lcsc": "C2934569",
                },
            ),
        ]
        self.convs = [
            make_ref(
                id=40,
                kind="conv",
                slug="discord/111/222/333",
                title="A thread",
            ),
        ]
        # Canned turns for conv id=40, keyed by ref_id. Blocks expose
        # pos / text / meta (author, ts) like the real Block dataclass.
        self._conv_blocks: dict[int, list[Any]] = {
            40: [
                SimpleNamespace(
                    pos=0,
                    text="hello there",
                    meta={"author": "alice", "ts": "2026-06-14T20:00:00Z"},
                ),
                SimpleNamespace(
                    pos=1,
                    text="general kenobi",
                    meta={"author": "bob", "ts": "2026-06-14T20:01:00Z"},
                ),
            ]
        }

    def _for_kind(self, kind: str | None) -> list[Any]:
        return {
            "todo": self.todos,
            "paper": self.papers,
            "memory": self.memories,
            "oracle": self.oracles,
            "conv": self.convs,
            "web": self.webs,
            "job": self.jobs,
            "pres": self.press,
            "datasheet": self.datasheets,
        }.get(kind or "", [])

    def list_blocks_for_ref(self, ref_id: int, **kw: Any) -> list[Any]:
        return list(self._conv_blocks.get(ref_id, []))

    def fetch_ref_ids_by_slugs(self, slugs, *, kind: str):
        """Resolve cite_key slugs → ref ids (the slug-addressed detail
        route). Live papers in the fixture pool only."""
        wanted = {s for s in slugs if s}
        return [r.id for r in self._for_kind(kind) if r.slug in wanted]

    def chunk_pages(self, ref_id: int, ords) -> dict[int, int]:
        """No page provenance in the fake corpus — sidebar nav still
        works, the PDF jump just has no page hint."""
        return {}

    def bump_salience_for_ref(self, ref_id: int) -> int:
        """Record the reader's on-open heat bump so a test can assert the
        just-viewed document was surfaced to the summarize hot tier."""
        self.salience_bumps.append(ref_id)
        return 0

    def chunk_summaries_for(self, ref_id: int, ords) -> dict[int, str]:
        """No llm-v1 glosses in the fake corpus — search rows carry an
        empty summary and the client falls back to keyword chips."""
        return {}

    def chunk_glosses_for_ref(self, ref_id: int, **kw: Any) -> list[dict[str, Any]]:
        """Per-chunk gloss list for the rapid-nav /chunks endpoint. Empty
        in the fake corpus (no body chunks); the contract is a list."""
        return []

    def search_blocks_semantic(self, *, query_vec, scope_ref_id=None, limit=20, **kw):
        """Return canned (block, ref, distance) hits for the paper-nav
        search route. Tests populate ``self.nav_hits`` per ref."""
        return list(self.nav_hits.get(scope_ref_id, []))

    def search_blocks_lexical(self, *, q, scope_ref_id=None, limit=20, **kw):
        """Keyword path — same canned hits as the semantic path so the
        route's result-shaping is exercised either way."""
        return list(self.nav_hits.get(scope_ref_id, []))

    def ref_ids_with_chunks(self, ref_ids) -> set[int]:
        # Fake corpus has no body chunks — the "has chunks" badge/filter
        # degrades to "none ingested". Routes handle the empty set.
        return set()

    def count_blocks(self, ref_id: int) -> int:
        return len(self._conv_blocks.get(ref_id, []))

    def links_for(self, ref_id, *, direction="both", relation=None):
        # No follow-up discussions in the fake — detail pages render the
        # empty Discussion state.
        return []

    def get_ref(self, *, kind: str, id):
        for r in (
            self.todos
            + self.papers
            + self.memories
            + self.oracles
            + self.convs
            + self.webs
            + self.jobs
            + self.press
            + self.datasheets
        ):
            if r.kind == kind and (r.slug == id or r.id == id):
                return r
        # The follow-up route ``put``s a conv via the (faked) runtime
        # dispatch, then resolves the slug → ref here. The fake dispatch
        # doesn't actually create the row, so synthesise the conv for
        # ``followup/`` slugs with a deterministic id.
        if kind == "conv" and isinstance(id, str) and id.startswith("followup/"):
            return make_ref(id=900000, kind="conv", slug=id, title="Follow-up")
        return None

    def resolve_handle(self, handle: str, *, conn: Any = None):
        """Decode a universal handle (ADR 0036) against the fixture pools.

        Record handles (``pa10``) resolve via the per-kind pool; chunk
        handles (``pc…``) via ``self.chunk_handles`` (``chunk_id ->
        (ref_id, ord, kind)``, populated by tests). Mirrors the real
        store's kind-match guard + ``None``-on-miss contract so the
        console resolver's handle branch can be exercised.
        """
        from precis.store.types import ResolvedHandle
        from precis.utils import handle_registry

        parsed = handle_registry.parse(handle)
        if parsed is None:
            return None
        kind, is_chunk, pk = parsed
        if is_chunk:
            info = self.chunk_handles.get(pk)
            if info is None or info[2] != kind:
                return None
            ref_id, ord_, row_kind = info
            ref = next((r for r in self._for_kind(kind) if r.id == ref_id), None)
            slug = getattr(ref, "slug", None) if ref is not None else None
            return ResolvedHandle(
                ref_id=ref_id,
                kind=kind,
                public_id=slug or str(ref_id),
                chunk_id=pk,
                chunk_ord=ord_,
            )
        ref = next((r for r in self._for_kind(kind) if r.id == pk), None)
        if ref is None:
            return None
        slug = getattr(ref, "slug", None)
        return ResolvedHandle(ref_id=pk, kind=kind, public_id=slug or str(pk))

    def list_refs(
        self,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
        **kw: Any,
    ):
        return list(self._for_kind(kind))[offset : offset + limit]

    def touch_viewed(self, ref_id: int) -> None:
        # The reader stamps last_viewed_at on page open; record the ids so a
        # test can assert the access was registered.
        self.viewed.append(ref_id)

    def live_paper_cites(self, handles: set[str], slugs: set[str]) -> set[str]:
        # Draft-reader local-vs-external citation colouring. The fake pool
        # parses no SQL, so default to "every cite is local" (unchanged sky
        # §); DraftFakeStore overrides to exercise the external ↗ branch.
        return set(handles) | set(slugs)

    def count_refs(
        self,
        *,
        kind: str | None = None,
        provider: str | None = None,
        tags: list[str] | None = None,
        **kw: Any,
    ) -> int:
        # Mirrors list_refs: this fake ignores tag filtering, so the
        # count matches the unfiltered per-kind pool the triage route
        # paginates over.
        return len(self._for_kind(kind))

    def search_refs_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ):
        return [(r, 1.0) for r in self._for_kind(kind)[:limit]]

    def fetch_refs_by_ids(self, ids, *, include_deleted: bool = False):
        pool = {
            r.id: r
            for r in self.todos
            + self.papers
            + self.memories
            + self.oracles
            + self.convs
            + self.webs
            + self.jobs
            + self.press
            + self.datasheets
        }
        return {i: pool[i] for i in ids if i in pool}

    def soft_delete_ref(self, ref_id, *, conn=None):
        """Record the soft-delete; raise NotFound on a repeat (mirrors the
        real store's ``deleted_at IS NULL`` guard) so the route's error
        branch can be exercised."""
        from precis.errors import NotFound

        if ref_id in self.deleted_ref_ids:
            raise NotFound(f"ref id={ref_id} not found (or already deleted)")
        self.deleted_ref_ids.add(ref_id)

    def merge_refs(self, victim_ref_id, survivor_ref_id):
        """Record a duplicate-merge: soft-delete the victim (mirrors the real
        store's atomic migrate-links + free-identifiers + soft-delete) and
        log the pair so the resolve-duplicate route's two directions can be
        asserted. Returns a canned migrated-link count."""
        from precis.errors import BadInput

        if victim_ref_id == survivor_ref_id:
            raise BadInput("cannot merge a ref into itself")
        self.merges.append((victim_ref_id, survivor_ref_id))
        self.soft_delete_ref(victim_ref_id)
        return 0

    def set_ref_identifier(
        self, ref_id, scheme, value, *, source="web-edit", conn=None
    ):
        """Record the identifier write; raise BadInput on a taken cite_key
        (mirrors the real store's cross-ref uniqueness guard) and reflect a
        cite_key change onto the ref's slug."""
        from precis.errors import BadInput

        v = value.strip().lower()
        if scheme == "cite_key" and v in self.taken_cite_keys:
            raise BadInput(f"{scheme}={v!r} already belongs to ref id=999")
        self.identifier_writes.append((ref_id, scheme, v))
        if scheme == "cite_key":
            for r in self.papers:
                if r.id == ref_id:
                    r.slug = v
        return True

    def suggest_cite_key(self, authors, year, *, exclude_ref_id=None, conn=None):
        """Real suggestion logic with no DB-backed collision probe — enough
        to render the suggestion hint in detail-page tests."""
        from precis.identity import make_cite_key

        return make_cite_key(authors, year)

    def abstract_previews(self, ref_ids, *, max_chars: int = 900):
        # Stand in for the leading-chunk backfill: only paper 11 has a
        # body-derived abstract under the fake.
        canned = {11: "Body-derived abstract text for the second paper."}
        return {i: canned[i] for i in ref_ids if i in canned}

    def ref_cite_keys(self, ref_id, *, conn=None):
        """All cite_key aliases for a ref: its current slug plus any canned
        extra aliases. Paper 11 carries a second alias (``jonesalt25``) filed
        under a different shard than its display slug — the multi-alias PDF
        resolver regression case."""
        extra = {11: ["jonesalt25"]}
        r = next((p for p in self.papers if p.id == ref_id), None)
        keys = [r.slug] if r is not None and r.slug else []
        return keys + extra.get(ref_id, [])

    def pdf_storage_path(self, pdf_sha256, *, conn=None):
        """The seeded authoritative path (``resolve_pdf_for_ref`` prefers it),
        or ``None`` so resolution falls back to the cite_key convention."""
        return self.storage_paths.get(pdf_sha256) or None

    def set_pdf_storage_path(self, pdf_sha256, path, *, conn=None):
        """Record + reflect a storage_path correction (the /rename path)."""
        if not pdf_sha256 or not path:
            return False
        self.storage_paths[pdf_sha256] = path
        self.storage_path_writes.append((pdf_sha256, path))
        return True

    def pdf_missing(self, pdf_sha256, *, ttl_days=None):
        """Ledger verdict for the draft reader's held-but-missing ▲ —
        False unless a test seeds the sha into ``missing_pdf_shas``."""
        return pdf_sha256 in self.missing_pdf_shas

    def identifiers_for_refs(self, ref_ids):
        # Paper 10 carries a DOI; paper 11 an arXiv id — exercises both
        # hover-card link branches.
        canned = {
            10: {"doi": "10.1234/example.2024"},
            11: {"arxiv": "2501.01234"},
        }
        return {i: canned[i] for i in ref_ids if i in canned}

    def tags_for(self, ref_id, *, pos=None):
        """Empty tag list — refs detail-page tag strip renders the
        ``no tags yet`` empty state. Routes that exercise add/remove
        path through the fake runtime call recorder, not this method.

        Two exceptions seed the job detail actions strip: the failed
        plan_tick job (id=80) carries ``STATUS:failed`` +
        ``swept:claim-orphaned``, and its planner parent (id=81) carries
        an ``LLM:opus`` tag so the retry model dropdown appears."""
        from precis.store import Tag

        if ref_id == 80:
            return [
                Tag.parse_strict("STATUS:failed", kind="job"),
                Tag.open("swept:claim-orphaned"),
            ]
        if ref_id == 81:
            return [Tag.parse_strict("LLM:opus", kind="todo")]
        return []

    def has_tag(self, ref_id, namespace, value):
        """Minimal presence probe — only models OPEN:needs-triage via the
        ``triaged_ref_ids`` set that tests populate."""
        if namespace == "OPEN" and value == "needs-triage":
            return ref_id in self.triaged_ref_ids
        return False

    def ref_tag_values(self, ref_ids, namespace, values):
        """Batched flag-state probe over ``ref_open_values`` (namespace
        ignored — the fake only models the OPEN flag axis)."""
        want = set(values)
        out: dict[int, set[str]] = {}
        for rid in ref_ids:
            present = self.ref_open_values.get(rid, set()) & want
            if present:
                out[rid] = present
        return out

    def search_chunks_across_kinds(self, *, kinds, q, **_kw):
        """Canned cross-kind hits for the /items page — one paper + one
        web ref, filtered to the requested kinds. Tests override the raw
        triples via ``self.cross_kind_hits`` and read the applied tag
        filter via ``self.search_tags`` / kind set via ``self.search_kinds``."""
        self.search_tags = _kw.get("tags")
        self.search_kinds = list(kinds)
        hits = getattr(self, "cross_kind_hits", None)
        if hits is None:
            pref = make_ref(id=10, kind="paper", slug="smith2024", title="A paper")
            wref = make_ref(
                id=70, kind="web", slug="example.com/page", title="A web page"
            )
            blk_p = SimpleNamespace(id=1001, pos=3, text="passage about the query")
            blk_w = SimpleNamespace(id=1002, pos=0, text="web snippet about the query")
            hits = [(blk_p, pref, 0.9), (blk_w, wref, 0.8)]
        want = set(kinds)
        return [(b, r, s) for (b, r, s) in hits if r.kind in want]

    def recent_refs(self, kinds, *, tags=None, has_pdf=None, limit=30):
        """Canned recent source refs for the /items default landing —
        one paper (stub, no pdf) + one web, filtered to requested kinds.
        ``self.recent_tags`` / ``self.recent_has_pdf`` record the filters."""
        self.recent_tags = tags
        self.recent_has_pdf = has_pdf
        src = [
            make_ref(id=10, kind="paper", slug="smith2024", title="A paper"),
            make_ref(id=70, kind="web", slug="example.com/page", title="A web page"),
        ]
        want = set(kinds)
        return [r for r in src if r.kind in want][:limit]

    def suggest_tags(self, q, *, limit=10):
        """Canned substring tag suggestions for the /items autocomplete."""
        pool = [("topic", "co2-capture", 42), ("topic", "graphene", 17)]
        ql = (q or "").lower()
        return [(ns, v, n) for (ns, v, n) in pool if ql in f"{ns}:{v}".lower()][:limit]

    def refs_with_body_chunks(self, ref_ids):
        """Which refs the fake reports as ingested. Tests populate
        ``self.ingested_ref_ids``; default empty (all look like stubs)."""
        return {
            rid for rid in ref_ids if rid in getattr(self, "ingested_ref_ids", set())
        }

    def ref_tags_bulk(self, ref_ids):
        """Canned per-ref tags for the /items row chips. Paper #10 carries
        a topical tag + a flag + a machine tag so the display filter is
        exercised (only the topical one should render as a chip)."""
        canned = {
            10: [("topic", "co2-capture"), ("OPEN", "read-later"), ("DREAM", "spec")],
        }
        return {rid: canned[rid] for rid in ref_ids if rid in canned}

    def paper_identifiers(self, ref_ids):
        """Canned identifiers for the /items UoL/Scholar links — paper #10
        carries a DOI so its find: links render."""
        return {rid: "10.1038/nature01797" for rid in ref_ids if rid == 10}

    def list_all_tags(self, *, kind=None, page=1, page_size=50):
        """Canned tag-usage rows for the /items tag cloud. A machine
        namespace (DREAM) is included so the exclusion filter is exercised."""
        return [
            ("topic", "carbon-capture", 42),
            ("topic", "graphene", 17),
            ("OPEN", "read-later", 5),
            ("DREAM", "speculative", 999),
        ][:page_size]

    def ingest_timestamps(self, ref_id: int):
        # Canned ingest timeline for the paper detail page. tz-aware
        # datetimes (any may be None for a stub / un-chunked paper).
        from datetime import UTC, datetime

        if ref_id == 10:
            return {
                "ref": datetime(2026, 6, 14, 9, 0, tzinfo=UTC),
                "pdf": datetime(2026, 6, 14, 9, 5, tzinfo=UTC),
                "first_chunk": datetime(2026, 6, 14, 9, 7, tzinfo=UTC),
            }
        return {"ref": None, "pdf": None, "first_chunk": None}

    def stub_backlog(self, *, limit: int = 50, offset: int = 0, awaiting: bool = False):
        # Two canned stubs: one never-attempted (always shown),
        # one attempted >24h ago with a failure (shown in both views).
        all_rows = [
            {
                "ref_id": 90,
                "cite_key": "javey2003",
                "identifier": "10.1038/nature01797",
                "last_attempt": "",
                "last_source": "",
                "last_event": "",
                "state": "never attempted",
                "created_at": "2026-07-01T08:00:00+00:00",
                "requested_by": "dream",
                "attempts": 0,
            },
            {
                "ref_id": 91,
                "cite_key": "novoselov2004",
                "identifier": "arxiv:cond-mat/0410550",
                "last_attempt": "2026-06-13T10:00:00+00:00",
                "last_source": "fetcher:unpaywall",
                "last_event": "no_oa_version",
                "state": "no OA version (24h ago)",
                "created_at": "2026-06-10T09:00:00+00:00",
                "requested_by": "system",
                "attempts": 3,
            },
        ]
        # ``awaiting`` filtering — in the real query, recent fetch_ok
        # would be excluded; both canned rows are awaiting here.
        return all_rows[offset : offset + limit]

    def stub_backlog_count(self, *, awaiting: bool = False) -> int:
        # Mirrors the two canned rows above (both awaiting under the fake).
        return 2

    def locked_ref_ids(self, ref_ids):
        # No live Postgres locks under the fake; the Tasks tab's
        # processing probe degrades to "nothing locked".
        return set()

    def events_for(self, ref_id, *, limit: int = 100, **kw: Any):
        # One canned status:done event so the history fragment has a
        # row to render; other refs return an empty log.
        if ref_id == 2:
            from datetime import UTC, datetime

            return [
                SimpleNamespace(
                    ts=datetime(2026, 6, 14, 20, 0, tzinfo=UTC),
                    event="status:done",
                    source="web:owner",
                )
            ]
        return []


class FakeRuntime:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        #: Some draft routes reach ``runtime.hub.embedder`` for query
        #: embedding; tests assign it ad hoc. Declared here so that
        #: assignment type-checks (else mypy flags "no attribute hub").
        self.hub: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Verbs the fake should report as failures (is_error=True), so
        #: the error-surfacing routes can be exercised without a real
        #: handler raising. The body mimics a handler BadInput message.
        self.error_verbs: set[str] = set()

    def dispatch_with_status(self, verb: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((verb, dict(args)))
        if verb in self.error_verbs:
            return (f"invalid {verb}: rejected by handler", True)
        return (f"[{verb}] ok", False)


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(FakeStore())


@pytest.fixture
def client(runtime: FakeRuntime, tmp_path) -> TestClient:
    cfg = WebConfig(corpus_dir=tmp_path)
    app = create_app(runtime=runtime, web_config=cfg)
    return TestClient(app)
