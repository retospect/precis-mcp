"""Consideration verdicts — the general "checked, does not fit" record.

Spec me626852 (gr662069): a first-class, discoverable record that a scoped
memory-or-question was judged against a source — verdict, reason, reading
depth, passage evidence, actor/model/run/prompt and the UTC checked-at —
replacing per-run private ledgers (the quest lit-search's own is the first
consumer, gr592951).

Storage shape (decided under plan td626694): the CURRENT verdict rides the
scoped connection itself — one ``considered`` link (scope → source) whose
``meta`` carries the verdict block — and EVERY judgment lands an append-only
``considerations`` history row. Canonical-identity dedup keeps one edge per
(scope, source) pair — a judgment at another chunk endpoint of the same
ref pair supersedes the older edge, so the current verdict resolves at
ref-pair granularity; the history keeps each run's verdict, so one paper can
be rejected for one question and useful to another, and re-judging a pair
never collapses the earlier verdict.

Validity is revision-scoped: the door stamps ``meta.scope_sha`` /
``meta.source_sha`` at judgment time. The scope pin is always the scope
REF's ``precis_target_sha`` — the verdict judges the whole question, so
a retitle or an edit to any scope chunk is a material change whatever
passage grounded the edge (review td662210). The source pin tracks the
judged endpoint (the grounded chunk's content, else the ref). A verdict
is *current* while both shas still match; a material change leaves it
stale and queues it for reconsideration
(:meth:`ConsiderationMixin.consideration_reconsiderations`) — ordinary
edits never clear the history itself.

The edge's chunk endpoints are ``ON DELETE CASCADE`` (the ``links`` FKs),
so a routine chunk replacement — body chunks are append-only, DELETE +
INSERT — deletes a chunk-grounded ``considered`` edge out from under its
judgment. The reconsideration state is therefore derived independently of
the live link (:data:`ConsiderationMixin._SCOPED_SQL` unions live edges
with *orphan* judgments — the pair's latest unwithdrawn history row whose
edge is gone — reported ``stale='edge-lost'``). An explicit removal
through the link-delete doors is different: ``remove_link`` /
``delete_links`` stamp ``withdrawn_at`` on every judgment of the pair via
:meth:`ConsiderationMixin.withdraw_considerations`, so a deliberate
withdrawal leaves the queue while a cascade loss stays in it.

``depth='none'`` is acquisition state, not rejection: a judge that never
saw text can only record ``uncertain`` (the door refuses useful/not-useful,
the table CHECK backs it), and such a verdict is never consumed as an
exclusion — it marks "acquire the text, then judge". Contrary evidence is
``useful``, never a negative verdict.

Cheap triage proposes pairs but never writes a durable negative verdict by
omission: nothing enters ``considerations`` except an explicit judgment
through the ``rel='considered'`` write door (:func:`validate_considered_meta`
inside :meth:`LinksMixin.add_link`).

Consumers: ``search(args={'novel_for': <scope>})`` merges valid scoped
exclusions across every search leg (dispatch facet, gr662069);
``get(kind='memory', id=<scope>, view='considerations')`` renders the
ledger with inspect/reconsider access. Global search, direct lookup and
other memories' scopes are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput

#: The scoped memory-or-question → source relation (migration 0320).
CONSIDER_RELATION = "considered"

#: Outcome vocabulary — useful / not-useful / uncertain (me626852).
CONSIDER_VERDICTS: tuple[str, ...] = ("useful", "not-useful", "uncertain")

#: How far the judge read, weakest to strongest. ``none`` is acquisition
#: state — missing text, never a rejection.
CONSIDER_DEPTHS: tuple[str, ...] = (
    "none",
    "metadata",
    "abstract",
    "partial",
    "fulltext",
    "passages",
)

#: ``links.meta`` keys the record door stamps itself — a caller-supplied
#: one would forge a revision pin or a judgment count.
CONSIDERED_STAMPED_KEYS: frozenset[str] = frozenset(
    {"scope_sha", "source_sha", "judgments"}
)

#: The meta keys that make up the judgment itself; anything else on the
#: edge meta passes through as caller context. A re-judgment REPLACEs
#: these keys on the edge (the conflict path strips the old block before
#: overlaying the new one) — an omitted optional key is gone, never
#: silently retained from the previous judgment (gr662069 review).
CONSIDER_JUDGMENT_KEYS: frozenset[str] = frozenset(
    {
        "verdict",
        "reason",
        "depth",
        "passages",
        "actor",
        "model",
        "run",
        "prompt",
        "checked_at",
    }
)


@dataclass(frozen=True, slots=True)
class ScopedVerdict:
    """One ``considered`` edge on a scope, with its read-time validity.

    ``valid`` = the recorded verdict still applies: both endpoint shas
    match and the source is live. ``stale`` names why it is queued for
    reconsideration (``'scope'`` | ``'source'`` | ``'source-retired'`` |
    ``'edge-lost'``), else ``None``. ``'edge-lost'`` marks an *orphan*
    judgment — the ``considered`` edge carrying it was deleted without a
    withdrawal stamp (a chunk-replacement cascade or graph maintenance,
    never a door withdrawal), so the pair's latest unwithdrawn history row
    stands in for the current verdict until it is re-judged.
    """

    link_id: int
    dst_ref_id: int
    dst_chunk_id: int | None
    dst_kind: str
    verdict: str
    depth: str
    reason: str
    checked_at: str | None
    judgments: int
    valid: bool
    stale: str | None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScopedExclusions:
    """What a novelty search consumes for one scope (gr662069).

    ``excluded`` — source ref_ids carrying a *valid* ``not-useful``
    verdict; the search drops them across every leg. ``stale`` — verdicts
    whose scope or source changed since judgment, plus ``edge-lost``
    orphans whose edge died without a withdrawal (the reconsider queue);
    they exclude nothing. ``needs_text`` — ``depth='none'`` acquisition
    markers; also never exclusions.
    """

    excluded: frozenset[int]
    stale: tuple[ScopedVerdict, ...]
    needs_text: tuple[ScopedVerdict, ...]
    verdicts: tuple[ScopedVerdict, ...]


def _parse_checked_at(value: Any) -> str:
    """Normalise a caller's ``meta.checked_at`` to a UTC ISO label.

    The door stamps ``now()`` when the key is absent; a supplied value
    must parse as ISO-8601 and be tz-aware (a naive stamp would read as
    an unlabelled local time — timestamps are UTC, labelled).
    """
    if value is None:
        return datetime.now(UTC).isoformat()
    if not isinstance(value, str) or not value.strip():
        raise BadInput(
            f"considered meta.checked_at must be an ISO-8601 UTC string, got {value!r}",
            next="omit checked_at to stamp now, or pass e.g. '2026-11-13T12:00:00Z'",
        )
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise BadInput(
            f"considered meta.checked_at is not ISO-8601: {raw!r}",
            next="pass e.g. '2026-11-13T12:00:00Z'",
        ) from None
    if parsed.tzinfo is None:
        raise BadInput(
            f"considered meta.checked_at needs a UTC offset: {raw!r}",
            next="label it 'Z' or '+00:00' — precis timestamps are UTC",
        )
    return parsed.astimezone(UTC).isoformat()


def validate_considered_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """The ``rel='considered'`` admission contract, checked in ``add_link``.

    A judgment requires ``verdict`` (useful | not-useful | uncertain),
    ``reason`` (a real sentence — never the reviewed-obvious marker: a
    verdict IS its reason) and ``depth`` (how far the judge read).
    ``depth='none'`` pairs only with ``uncertain`` — missing text is
    acquisition state, not rejection. Optional ``passages`` (evidence
    handles/snippets), ``actor``/``model``/``run``/``prompt`` and
    ``checked_at`` are validated when present; stamped keys
    (:data:`CONSIDERED_STAMPED_KEYS`) are refused.
    """
    if not isinstance(meta, dict):
        raise BadInput(
            "rel='considered' records a judgment: meta is required",
            next=(
                "link(kind='memory', id=<scope>, target=<source>, "
                "rel='considered', reason='why', meta={'verdict': "
                "'not-useful', 'depth': 'abstract'})"
            ),
        )
    forged = CONSIDERED_STAMPED_KEYS & meta.keys()
    if forged:
        raise BadInput(
            f"considered meta keys {sorted(forged)} are stamped by the "
            "write door (endpoint shas and the judgment count)",
            next="drop them — a re-judgment re-stamps the pins it checked",
        )
    verdict = meta.get("verdict")
    if verdict not in CONSIDER_VERDICTS:
        raise BadInput(
            f"considered meta.verdict must be one of {CONSIDER_VERDICTS}, "
            f"got {verdict!r}",
            options=list(CONSIDER_VERDICTS),
        )
    reason = meta.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or reason.strip()
        in (
            ".",
            "reviewed-obvious",
        )
    ):
        raise BadInput(
            "considered requires a real reason — the verdict IS its "
            "'not a match because …'; the reviewed-obvious marker '.' "
            "records nothing",
            next="reason='why this source does not fit the scope' or "
            "meta={'reason': '…'}",
        )
    depth = meta.get("depth")
    if depth not in CONSIDER_DEPTHS:
        raise BadInput(
            f"considered meta.depth must be one of {CONSIDER_DEPTHS}, got {depth!r}",
            options=list(CONSIDER_DEPTHS),
        )
    if depth == "none" and verdict != "uncertain":
        raise BadInput(
            "considered: depth='none' is acquisition state, not a "
            "judgment — missing text can only record verdict='uncertain'",
            next="acquire the text first, or record verdict='uncertain' "
            "with depth='none' as the needs-text marker",
        )
    passages = meta.get("passages")
    if passages is not None and not isinstance(passages, list):
        raise BadInput(
            f"considered meta.passages must be a list of evidence "
            f"handles or quoted spans, got {passages!r}",
            next="meta={'passages': ['pc<chunk>', …]}",
        )
    for key in ("actor", "model", "run", "prompt"):
        value = meta.get(key)
        if value is not None and not isinstance(value, str):
            raise BadInput(
                f"considered meta.{key} must be a string, got {value!r}",
            )
    out = dict(meta)
    out["checked_at"] = _parse_checked_at(meta.get("checked_at"))
    return out


def _target_sha(c: Connection, ref_id: int, chunk_id: int | None) -> str | None:
    """``precis_target_sha`` of an edge endpoint (chunk-grounded or ref)."""
    row = c.execute(
        "SELECT precis_target_sha(%s, %s)",
        ("chunk" if chunk_id is not None else "ref", chunk_id or ref_id),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def lock_consideration_pair(
    c: Connection, scope_ref_id: int, source_ref_id: int
) -> None:
    """Serialize judgment writes per (scope, source) ref pair.

    Two concurrent ``rel='considered'`` writes at DIFFERENT chunk
    endpoints hit no unique-index conflict, and under READ COMMITTED
    each transaction's supersede-DELETE (in
    :meth:`ConsiderationMixin.record_consideration`) cannot see the
    other's uncommitted edge — both would commit live ``considered``
    edges for the pair, letting a superseded ``not-useful`` keep
    excluding beside the newer verdict (review td662210). Held to
    commit, this xact-scoped advisory lock makes the later write
    observe the earlier edge, so exactly one carries the pair's current
    verdict. A judgment takes exactly one pair key, so there is no
    lock-ordering hazard. Key is a single bigint via
    ``hashtextextended`` (the ``_draft_ops`` convention) — the
    ``(int4, int4)`` overload would collide with per-ref namespaces
    that key on a lone id.
    """
    c.execute(
        "SELECT pg_advisory_xact_lock("
        "hashtextextended('considered-pair:' || %s || ':' || %s, 0))",
        (scope_ref_id, source_ref_id),
    )


def stamp_considered(
    c: Connection,
    *,
    src_ref_id: int,
    src_chunk_id: int | None,
    dst_ref_id: int,
    dst_chunk_id: int | None,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Add the door-stamped revision pins to a validated considered meta.

    ``scope_sha`` pins the scope REF's revision even when the edge is
    chunk-grounded: the verdict judges the whole question, so a retitle
    or an edit to another scope chunk must reopen review — a chunk pin
    would leave the negative verdict valid through exactly that change
    (review td662210). ``source_sha`` pins the judged endpoint — the
    grounded chunk's content for a passage verdict, else the ref.
    """
    out = dict(meta)
    out["scope_sha"] = _target_sha(c, src_ref_id, None)
    out["source_sha"] = _target_sha(c, dst_ref_id, dst_chunk_id)
    return out


class ConsiderationMixin:
    """Read/write door for the ``considered`` verdict ledger (gr662069)."""

    pool: ConnectionPool

    def record_consideration(
        self,
        c: Connection,
        *,
        link_id: int | None,
        src_ref_id: int,
        src_chunk_id: int | None,
        dst_ref_id: int,
        dst_chunk_id: int | None,
        meta: dict[str, Any],
        set_by: str,
    ) -> int:
        """Append one judgment to ``considerations`` and refresh the
        edge's ``meta.judgments`` count. Runs inside the ``add_link``
        transaction, after the edge INSERT; history is kept per judgment
        — canonical-identity dedup never collapses two runs' verdicts."""
        checked_at = _parse_checked_at(meta.get("checked_at"))
        row = c.execute(
            "INSERT INTO considerations "
            "  (link_id, scope_ref_id, scope_chunk_id, source_ref_id, "
            "   source_chunk_id, verdict, reason, reading_depth, passages, "
            "   scope_sha, source_sha, actor, model, run, prompt, set_by, "
            "   checked_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        %s, %s, %s, %s) "
            "RETURNING consideration_id",
            (
                link_id,
                src_ref_id,
                src_chunk_id,
                dst_ref_id,
                dst_chunk_id,
                meta["verdict"],
                meta["reason"].strip(),
                meta["depth"],
                Jsonb(meta.get("passages") or []),
                meta.get("scope_sha"),
                meta.get("source_sha"),
                meta.get("actor"),
                meta.get("model"),
                meta.get("run"),
                meta.get("prompt"),
                set_by,
                checked_at,
            ),
        ).fetchone()
        assert row is not None
        # The current verdict resolves at ref-pair granularity: a judgment
        # grounded at another chunk endpoint of the same scope/source refs
        # supersedes the older live edge(s) — otherwise "chunk A not-useful,
        # then chunk B useful" would still exclude the whole source. The
        # older edges are dropped (their judgments stay in the history and
        # retire below as superseded orphans).
        if link_id is not None:
            c.execute(
                "DELETE FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "  AND relation = 'considered' AND link_id <> %s",
                (src_ref_id, dst_ref_id, link_id),
            )
        # Retire superseded reconsideration state: earlier judgments of the
        # same scope/source ref pair whose edge is gone (orphans at an old
        # endpoint) are replaced by this one, so withdrawing the new edge
        # later cannot resurrect them.
        c.execute(
            "UPDATE considerations j SET withdrawn_at = now() "
            "WHERE j.scope_ref_id = %s AND j.source_ref_id = %s "
            "  AND j.consideration_id <> %s AND j.withdrawn_at IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM links l "
            "                   WHERE l.link_id = j.link_id)",
            (src_ref_id, dst_ref_id, row[0]),
        )
        if link_id is not None:
            c.execute(
                "UPDATE links SET meta = meta || jsonb_build_object("
                "  'judgments', ("
                "    SELECT count(*) FROM considerations j"
                "    WHERE j.scope_ref_id = %s"
                "      AND j.scope_chunk_id IS NOT DISTINCT FROM %s"
                "      AND j.source_ref_id = %s"
                "      AND j.source_chunk_id IS NOT DISTINCT FROM %s)) "
                "WHERE link_id = %s",
                (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, link_id),
            )
        return int(row[0])

    def consideration_history(
        self,
        *,
        scope_ref_id: int | None = None,
        source_ref_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
        conn: Connection | None = None,
    ) -> list[dict[str, Any]]:
        """The auditable judgment log, newest first. ``None`` filters
        widen to the whole ledger (bounded by ``limit``; ``offset`` pages
        into it)."""
        clauses = ["TRUE"]
        params: list[Any] = []
        if scope_ref_id is not None:
            clauses.append("j.scope_ref_id = %s")
            params.append(scope_ref_id)
        if source_ref_id is not None:
            clauses.append("j.source_ref_id = %s")
            params.append(source_ref_id)
        sql = (
            "SELECT j.*, sr.kind AS scope_kind, dr.kind AS source_kind "
            "FROM considerations j "
            "JOIN refs sr ON sr.ref_id = j.scope_ref_id "
            "JOIN refs dr ON dr.ref_id = j.source_ref_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY j.recorded_at DESC, j.consideration_id DESC "
            "LIMIT %s OFFSET %s"
        )
        params.extend((int(limit), int(offset)))

        def _do(c: Connection) -> list[dict[str, Any]]:
            with c.cursor(row_factory=dict_row) as cur:
                return list(cur.execute(sql, params).fetchall())

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def consideration_history_count(
        self,
        *,
        scope_ref_id: int | None = None,
        source_ref_id: int | None = None,
        conn: Connection | None = None,
    ) -> int:
        """The unwindowed size of :meth:`consideration_history`'s result
        — the inspect view pages against this total."""

        def _do(c: Connection) -> int:
            clauses = ["TRUE"]
            params: list[Any] = []
            if scope_ref_id is not None:
                clauses.append("scope_ref_id = %s")
                params.append(scope_ref_id)
            if source_ref_id is not None:
                clauses.append("source_ref_id = %s")
                params.append(source_ref_id)
            row = c.execute(
                f"SELECT count(*) FROM considerations WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
            return int(row[0]) if row is not None else 0

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    #: The scoped verdict read: live ``considered`` edges UNION orphan
    #: judgments — the latest *unwithdrawn* history row per
    #: (scope_chunk, source, source_chunk) pair whose edge is gone.
    #: Orphans exist because the links chunk FKs are ``ON DELETE
    #: CASCADE``: a routine chunk replacement deletes the edge but never
    #: the judgment, and the judgment must stay in the reconsider queue
    #: (review td662210). An explicit door removal stamps
    #: ``withdrawn_at`` instead — see :meth:`withdraw_considerations` —
    #: so a withdrawn pair surfaces no orphan. ``%(scope)s`` may be NULL
    #: (the unscoped reconsider queue).
    _SCOPED_SQL = """
        SELECT l.link_id, l.dst_ref_id, l.dst_chunk_id, l.meta,
               dr.kind AS dst_kind,
               (dr.retired_at IS NOT NULL) AS dst_retired,
               l.meta->>'scope_sha' IS NOT DISTINCT FROM
                 precis_target_sha('ref', l.src_ref_id)
                 AS scope_current,
               l.meta->>'source_sha' IS NOT DISTINCT FROM
                 CASE WHEN l.dst_chunk_id IS NULL
                      THEN precis_target_sha('ref', l.dst_ref_id)
                      ELSE precis_target_sha('chunk', l.dst_chunk_id) END
                 AS source_current,
               FALSE AS edge_lost
          FROM links l
          JOIN refs dr ON dr.ref_id = l.dst_ref_id
         WHERE (%(scope)s::bigint IS NULL OR l.src_ref_id = %(scope)s)
           AND l.relation = 'considered'
         UNION ALL
        SELECT o.link_id, o.source_ref_id, o.source_chunk_id,
               jsonb_build_object(
                 'verdict', o.verdict,
                 'reason', o.reason,
                 'depth', o.reading_depth,
                 'checked_at', o.checked_at,
                 'judgments', o.judgments,
                 'scope_sha', o.scope_sha,
                 'source_sha', o.source_sha),
               dr.kind,
               (dr.retired_at IS NOT NULL),
               FALSE, FALSE, TRUE
          FROM (
            SELECT DISTINCT ON
                   (j.scope_ref_id, j.scope_chunk_id,
                    j.source_ref_id, j.source_chunk_id)
                   j.link_id, j.scope_ref_id, j.scope_chunk_id,
                   j.source_ref_id, j.source_chunk_id,
                   j.verdict, j.reason, j.reading_depth, j.checked_at,
                   j.scope_sha, j.source_sha, jn.judgments
              FROM considerations j
              JOIN (
                SELECT scope_ref_id, scope_chunk_id,
                       source_ref_id, source_chunk_id,
                       count(*) AS judgments
                  FROM considerations
                 WHERE %(scope)s::bigint IS NULL
                    OR scope_ref_id = %(scope)s
                 GROUP BY 1, 2, 3, 4
              ) jn
                ON jn.scope_ref_id = j.scope_ref_id
               AND jn.scope_chunk_id IS NOT DISTINCT FROM j.scope_chunk_id
               AND jn.source_ref_id = j.source_ref_id
               AND jn.source_chunk_id IS NOT DISTINCT FROM j.source_chunk_id
             WHERE (%(scope)s::bigint IS NULL OR j.scope_ref_id = %(scope)s)
               AND j.withdrawn_at IS NULL
             ORDER BY j.scope_ref_id, j.scope_chunk_id,
                      j.source_ref_id, j.source_chunk_id,
                      j.recorded_at DESC, j.consideration_id DESC
          ) o
          JOIN refs dr ON dr.ref_id = o.source_ref_id
          -- Ref-pair granularity, not the dead chunk: a re-judgment
          -- grounds on the NEW chunk (or ref-level), so matching the
          -- orphan's exact 4-tuple could never suppress it — any live
          -- considered edge between the same two refs carries the
          -- pair's current verdict and resolves the orphan.
         WHERE NOT EXISTS (
           SELECT 1 FROM links l2
            WHERE l2.src_ref_id = o.scope_ref_id
              AND l2.dst_ref_id = o.source_ref_id
              AND l2.relation = 'considered')
         ORDER BY link_id
        """

    @staticmethod
    def _row_to_scoped(r: Any) -> ScopedVerdict:
        meta = r[3] or {}
        if r[5]:
            stale = "source-retired"
        elif r[8]:
            # An orphan judgment: the edge carrying it is gone (chunk
            # replacement or graph maintenance, not a withdrawal) — the
            # verdict can't pin a live endpoint sha, so it reopens as
            # 'edge-lost' until re-judged.
            stale = "edge-lost"
        elif not r[6]:
            stale = "scope"
        elif not r[7]:
            stale = "source"
        else:
            stale = None
        return ScopedVerdict(
            link_id=int(r[0]) if r[0] is not None else 0,
            dst_ref_id=int(r[1]),
            dst_chunk_id=int(r[2]) if r[2] is not None else None,
            dst_kind=str(r[4]),
            verdict=str(meta.get("verdict") or ""),
            depth=str(meta.get("depth") or ""),
            reason=str(meta.get("reason") or ""),
            checked_at=(
                str(meta.get("checked_at")) if meta.get("checked_at") else None
            ),
            judgments=int(meta.get("judgments") or 0),
            valid=stale is None,
            stale=stale,
            meta=dict(meta),
        )

    def withdraw_considerations(
        self,
        c: Connection,
        pairs: list[tuple[int, int | None, int, int | None]],
    ) -> int:
        """Stamp ``withdrawn_at`` on every judgment of the given
        (scope_ref, scope_chunk, source_ref, source_chunk) pairs — the
        explicit-door counterpart of a cascade edge loss. Called inside
        the ``remove_link`` / ``delete_links`` transaction after their
        DELETE RETURNING reports ``considered`` rows. Stamping ALL rows
        of the pair (not just the latest) keeps the whole ledger
        withdrawn; a re-judgment lands a fresh unwithdrawn row, so the
        pair's verdict ledger reactivates only through a new judgment.
        Returns the number of rows stamped."""
        n = 0
        for scope_ref, scope_chunk, source_ref, source_chunk in pairs:
            cur = c.execute(
                "UPDATE considerations SET withdrawn_at = now() "
                "WHERE scope_ref_id = %s "
                "  AND scope_chunk_id IS NOT DISTINCT FROM %s "
                "  AND source_ref_id = %s "
                "  AND source_chunk_id IS NOT DISTINCT FROM %s "
                "  AND withdrawn_at IS NULL",
                (scope_ref, scope_chunk, source_ref, source_chunk),
            )
            n += cur.rowcount or 0
            n += self.withdraw_consideration_pair(c, scope_ref, source_ref)
        return n

    def withdraw_consideration_pair(
        self, c: Connection, scope_ref_id: int, source_ref_id: int
    ) -> int:
        """Withdraw a scope→source pair's judgments at ref-pair
        granularity, independent of any live edge — the same grain
        :data:`_SCOPED_SQL` resolves orphans at. A chunk cascade can
        leave an orphan no live edge (hence no deleted row) can address,
        and a re-judgment at a new endpoint leaves the old endpoint's
        orphan behind; both retire here once no live ``considered`` edge
        remains for the pair. No-op (0) while one still carries the
        pair's current verdict."""
        cur = c.execute(
            "UPDATE considerations SET withdrawn_at = now() "
            "WHERE scope_ref_id = %s AND source_ref_id = %s "
            "  AND withdrawn_at IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM links l "
            "                   WHERE l.src_ref_id = %s "
            "                     AND l.dst_ref_id = %s "
            "                     AND l.relation = 'considered')",
            (scope_ref_id, source_ref_id, scope_ref_id, source_ref_id),
        )
        return cur.rowcount or 0

    def scoped_considerations(
        self, scope_ref_id: int, *, conn: Connection | None = None
    ) -> list[ScopedVerdict]:
        """Every ``considered`` verdict on one scope — live edges plus
        orphan judgments whose edge died unwithdrawn — with read-time
        validity (stale = material scope/source change, a retired
        source, or an ``edge-lost`` cascade casualty — queued for
        reconsideration, never silently cleared)."""

        def _do(c: Connection) -> list[ScopedVerdict]:
            rows = c.execute(self._SCOPED_SQL, {"scope": scope_ref_id}).fetchall()
            return [self._row_to_scoped(r) for r in rows]

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def consideration_exclusions(
        self, scope_ref_id: int, *, conn: Connection | None = None
    ) -> ScopedExclusions:
        """The novelty-search consumption set for one scope: valid
        ``not-useful`` source ids to drop, plus the stale/reconsider and
        needs-text cohorts it must report (never exclude)."""
        verdicts = self.scoped_considerations(scope_ref_id, conn=conn)
        excluded = frozenset(
            v.dst_ref_id for v in verdicts if v.verdict == "not-useful" and v.valid
        )
        return ScopedExclusions(
            excluded=excluded,
            stale=tuple(v for v in verdicts if v.stale is not None),
            needs_text=tuple(v for v in verdicts if v.depth == "none" and v.valid),
            verdicts=tuple(verdicts),
        )

    def consideration_reconsiderations(
        self,
        *,
        scope_ref_id: int | None = None,
        conn: Connection | None = None,
    ) -> list[ScopedVerdict]:
        """The reconsider queue: ``considered`` verdicts gone stale —
        a material scope/source change, a retired source, or an edge
        lost to a chunk cascade since the judgment (an explicit
        withdrawal stamps ``withdrawn_at`` and leaves the queue).
        History is untouched; the queue is derived, so an ordinary edit
        never clears anything."""

        def _do(c: Connection) -> list[ScopedVerdict]:
            rows = c.execute(self._SCOPED_SQL, {"scope": scope_ref_id}).fetchall()
            return [v for v in (self._row_to_scoped(r) for r in rows) if v.stale]

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)
