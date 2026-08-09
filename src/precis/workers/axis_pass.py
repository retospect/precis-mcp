"""axis_pass — generic ``data/axes/<id>.yaml`` classifier runner.

Where ``classify.py`` hardcodes the junk/role3 cascade and
``classify_topics.py`` hardcodes the topic-dossier taxonomy, this module
parameterizes the same claim → LLM-classify → tag-write shape over *any*
axis definition:

* ``level: chunk`` (role3/junk-style) — claims eligible body paragraphs,
  leases each in ``chunk_claims`` (mirrors ``classify.py``), writes
  ``Tag.closed(NS, value)`` with ``pos=ord`` -> ``chunk_tags``.
* ``level: ref`` (domain/material-style — the default when an axis omits
  ``level:``) — claims ``refs`` by ``applies_to_kinds`` (default
  ``["paper"]``), no lease table (mirrors ``classify_topics.py``), writes
  ``Tag.closed(NS, value)`` with no ``pos`` -> ``ref_tags``.

**Prerequisite enforcement** (the new piece controlled chunk tagging asked for and
neither existing pass implements): an item is eligible for axis X only if
it already carries a tag in the namespace of every axis in X's
``prereq:`` list. Gating is keyed to the *prerequisite* axis's own
``level`` — a ref-level prereq (the common case, e.g. ``material``
gated on ``domain``) is checked via ``ref_tags`` on the item's (parent)
ref; a chunk-level prereq is checked via ``chunk_tags`` on the very same
chunk (only meaningful when this axis is itself chunk-level — no
currently-defined axis needs it, but the hook is generic). This is why a
chunk-level axis whose prereq is ref-level (e.g. a hypothetical
chunk-level axis gated on ``domain``) resolves against the *parent ref*,
not the chunk: the prereq's own level decides which table is checked.

**``applies_when:`` gate** (orthogonal to, and coexists with, ``prereq:`` —
both must pass): the three forms that appear in ``data/axes/*.yaml``
today. Absent, or ``always: true``, gates nothing. ``domain_in: [...]``
requires the item's (parent) ref to carry a ``DOMAIN`` tag whose VALUE is
in the list — stricter than a bare ``prereq: [domain]`` presence check,
since a ``DOMAIN:bio`` ref satisfies the prereq but fails
``domain_in: [physics, materials, eng]``. ``tags_any: ["NS:value", ...]``
requires at least one of the listed closed-prefix tags to be present:
resolved on the (parent) ref for a **ref-level** axis (``move``, the
dream/memory axis, gates on ``DREAM:speculative`` / ``DREAM:grounded``),
but on the **chunk itself** for a chunk-level axis — checked via
``v_chunk_tags_all`` (the chunk's own ``chunk_tags`` plus inherited
``ref_tags``), so a chunk axis can gate on another chunk axis's VALUE
(``open-question`` runs only on ``ROLE3:own`` / ``ROLE3:background``
chunks, skipping furniture). Unlike ``prereq:`` — which keys the table off
the *prerequisite* axis's level — ``tags_any`` keys off *this* axis's
level.

**Idempotency + versioning**: a per-axis marker tag ``f"{NS}CASCADE"``
(mirroring ``classify_topics``'s ``TOPICCASCADE``) valued at the current
``version`` is the done-check, at the same level as the classification
tag itself (chunk_tags with the same ``pos`` for a chunk-level axis;
ref_tags for a ref-level axis). Bumping the axis YAML's ``version`` (or
passing an explicit ``version=`` override) changes the marker VALUE the
claim excludes on, so already-tagged items become claimable again —
lazy, no backfill script needed. A call/parse failure writes neither the
value tag nor the marker. For a **ref-level** axis (no lease table) a
claim-time attempt lease (:mod:`precis.workers.ref_lease`, mirroring
``classify_topics``'s own fix) is written just before the LLM call and
survives a failure, braking the item from re-claim for a cooldown window
instead of every sweep — cleared again on the next successful classify, so
a version bump still reclaims promptly (OPEN-ITEMS "Unbraked LLM-pass
cluster"). For a **chunk-level** axis the ``chunk_claims`` lease taken at
claim time persists on failure, so a failed chunk is NOT retried until the
axis ``version`` is bumped — the same failure-lease behaviour as the
``classify`` cascade. A per-axis failed-lease reaper is a prerequisite
before any chunk-level axis is swept corpus-wide (tracked in OPEN-ITEMS).

Default-OFF: each axis registers under its own ``service_config`` service
name ``axis:<id>`` (``cli/worker.py``'s per-axis wiring), off unless a
``service_config`` row (or the ``PRECIS_AXES_ENABLED`` seed list) turns it
on — flip it live from the ``/categorizers`` console. See the wiring
block there + ``workers/registry.py``'s ``"axis"`` ``ServiceSpec``.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from precis.store.types import Tag
from precis.workers import ref_lease

_AXES_DIR = Path(__file__).resolve().parent.parent / "data" / "axes"
_ABSTRACT_CHARS = 2000

#: Axis ids that run under the ``classify`` cascade pass (``junk``-gate ->
#: ``role3``) instead of this generic runner — never
#: double-registered by :func:`discover_axis_ids`.
CASCADE_AXIS_IDS = frozenset({"junk", "role3"})


def discover_axis_ids() -> list[str]:
    """Every ``data/axes/*.yaml`` id this generic runner can drive.

    Skips files with no ``id:`` key (``journal_domains.yaml``, a journal->
    domain lookup table, not itself a categorizer) and the two ids the
    ``classify`` cascade already owns (:data:`CASCADE_AXIS_IDS`). Sorted for
    a deterministic registration order — the single source both
    ``cli/worker.py``'s per-axis pass wiring and the ``/categorizers``
    console read, so the two lists can't silently drift.
    """
    ids: list[str] = []
    for path in sorted(_AXES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            continue
        if isinstance(data, dict) and data.get("id"):
            axis_id = str(data["id"])
            if axis_id not in CASCADE_AXIS_IDS:
                ids.append(axis_id)
    return ids


_SYS = (
    "You are a precise single-label classifier. Reply with ONLY the "
    "requested JSON object, no prose."
)


def _load_axis(axis_id: str) -> dict[str, Any]:
    return yaml.safe_load((_AXES_DIR / f"{axis_id}.yaml").read_text())


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            parsed = json.loads(text[a : b + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _render_examples(axis: dict[str, Any]) -> str:
    ex = axis.get("examples") or []
    if not ex:
        return ""
    out = ["Worked examples (learn the boundaries):"]
    for e in ex:
        why = f"   # {e['why']}" if e.get("why") else ""
        out.append(f'- "{e["text"]}" -> {{"value": "{e["value"]}"}}{why}')
    return "\n".join(out) + "\n"


def _build_chunk_prompt(axis: dict[str, Any], row: dict[str, Any]) -> str:
    """Chunk context packet declared by the axis ``context:`` field.

    Mirrors ``classify.py``'s ``_build_prompt`` (kept as an independent
    copy rather than an import — this module must not couple its
    behaviour to classify.py's, per the additive-only brief)."""
    want = set(axis.get("context", []))
    lines: list[str] = []
    if "title" in want and row.get("title"):
        lines.append(f"Paper title: {row['title']}")
    if "section_path" in want and row.get("section_path"):
        lines.append(f"Section: {row['section_path']}")
    if "position" in want and row.get("position"):
        lines.append(f"Position in document: {row['position']}")
    if "neighbor_gists_1" in want:
        if row.get("prev_gist"):
            lines.append(f"Previous chunk (gist): {row['prev_gist']}")
        if row.get("next_gist"):
            lines.append(f"Next chunk (gist): {row['next_gist']}")
    lines.append("")
    lines.append(f"CHUNK TEXT:\n{row.get('text', '')}")
    ex = _render_examples(axis)
    ex_block = f"\n{ex}\n" if ex else "\n"
    return f"{axis['prompt'].rstrip()}\n{ex_block}---\n" + "\n".join(lines) + "\n"


def _build_ref_prompt(axis: dict[str, Any], row: dict[str, Any]) -> str:
    """Title + abstract packet for a ref-level axis (domain/material-style)."""
    lines = [
        f"Paper title: {row.get('title', '')}",
        "",
        f"Abstract:\n{(row.get('abstract') or '')[:_ABSTRACT_CHARS]}",
    ]
    ex = _render_examples(axis)
    ex_block = f"\n{ex}\n" if ex else "\n"
    return f"{axis['prompt'].rstrip()}\n{ex_block}---\n" + "\n".join(lines) + "\n"


def prompt_preview(axis_id: str) -> dict[str, str]:
    """The actual (system, user) prompt this axis sends the LLM, built with
    placeholder paper content — for the ``/categorizers`` hover popover.
    Reuses the real :func:`_build_ref_prompt` / :func:`_build_chunk_prompt` so
    the preview can't drift from what the pass actually sends."""
    axis = _load_axis(axis_id)
    if axis.get("level", "ref") == "chunk":
        row = {
            "title": "‹paper title›",
            "section_path": "‹section ▸ path›",
            "position": "‹n/N›",
            "prev_gist": "‹previous chunk gist›",
            "next_gist": "‹next chunk gist›",
            "text": "‹the chunk text being classified›",
        }
        user = _build_chunk_prompt(axis, row)
    else:
        row = {"title": "‹paper title›", "abstract": "‹paper abstract / opening text›"}
        user = _build_ref_prompt(axis, row)
    return {"system": _SYS, "user": user}


def _classify_one(dispatch: Any, axis: dict[str, Any], prompt: str) -> str | None:
    """Returns the raw ``value`` string from the model, or ``None`` on a
    call/parse failure (the caller decides whether an out-of-vocabulary
    value falls back to ``default_unknown`` or also counts as failed)."""
    try:
        out = dispatch.complete(
            [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": prompt},
            ]
        )
    except Exception:
        return None
    parsed = _extract_json(out.text)
    if parsed is None:
        return None
    val = parsed.get("value")
    return val if isinstance(val, str) else None


# ── prereq gating ───────────────────────────────────────────────────────


def _prereq_clauses(
    axis: dict[str, Any], *, ref_col: str, chunk_col: str | None
) -> tuple[str, dict[str, Any]]:
    """``AND EXISTS (...)`` clauses gating claim eligibility on ``prereq:``.

    Each listed prereq id must already carry a tag in its own namespace —
    checked at the level the *prerequisite* axis itself runs at: a
    ref-level prereq (the common case, e.g. ``material`` gated on
    ``domain``) is checked via ``ref_tags`` on ``ref_col`` (the item's own
    ref_id, or its parent ref when the axis being gated is chunk-level); a
    chunk-level prereq is checked via ``chunk_tags`` on ``chunk_col`` (the
    very same chunk — only valid when the axis being gated is itself
    chunk-level, since there is no single "the chunk" for a ref-level
    target).
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, prereq_id in enumerate(axis.get("prereq") or []):
        prereq_axis = _load_axis(prereq_id)
        prereq_ns = prereq_id.upper()
        key = f"prereq_ns_{i}"
        params[key] = prereq_ns
        if prereq_axis.get("level", "ref") == "chunk":
            if chunk_col is None:
                raise ValueError(
                    f"axis prereq {prereq_id!r} is chunk-level but this "
                    "axis is ref-level — no single chunk to check"
                )
            clauses.append(
                f"AND EXISTS (SELECT 1 FROM chunk_tags pct{i} "
                f"JOIN tags pt{i} ON pt{i}.tag_id = pct{i}.tag_id "
                f"WHERE pct{i}.chunk_id = {chunk_col} "
                f"AND pt{i}.namespace = %({key})s)"
            )
        else:
            clauses.append(
                f"AND EXISTS (SELECT 1 FROM ref_tags prt{i} "
                f"JOIN tags pt{i} ON pt{i}.tag_id = prt{i}.tag_id "
                f"WHERE prt{i}.ref_id = {ref_col} "
                f"AND pt{i}.namespace = %({key})s)"
            )
    return "\n        ".join(clauses), params


def _applies_when_clauses(
    axis: dict[str, Any], *, ref_col: str, chunk_col: str | None = None
) -> tuple[str, dict[str, Any]]:
    """``AND EXISTS (...)`` clause(s) for the axis's ``applies_when:`` gate —
    an orthogonal layer alongside ``prereq:`` (both must pass; an item can
    satisfy a bare prereq presence-check yet still fail a stricter
    ``applies_when`` value check, e.g. ``DOMAIN:bio`` satisfies
    ``prereq: [domain]`` but fails ``domain_in: [physics, materials,
    eng]``). Handles the three forms that appear in ``data/axes/*.yaml``
    today:

    - absent, or ``always: true`` -> no clause.
    - ``domain_in: [...]`` -> the (parent) ref must carry a ``DOMAIN`` tag
      whose VALUE is in the list. Always ref-level (``DOMAIN`` is a ref
      axis), resolved against ``ref_col``.
    - ``tags_any: ["NS:value", ...]`` -> at least one of the listed
      closed-prefix ``(namespace, value)`` tags is present (each token
      split on the first ``:``). Built as an OR of per-pair equality
      checks rather than a single row-value ``IN`` — safer against driver
      tuple-array adaptation than assuming composite-type support. For a
      **chunk-level axis** (``chunk_col`` given), this resolves against
      ``v_chunk_tags_all`` on ``chunk_col`` — the view unions the chunk's
      own ``chunk_tags`` with its inherited ``ref_tags``, so a token may
      name a chunk axis (e.g. ``ROLE3:own`` gating ``open-question``) or a
      ref axis, both read from the chunk's perspective. Ref-level axes
      keep the ``ref_tags``-on-``ref_col`` path.
    """
    when = axis.get("applies_when") or {}
    if not when or when.get("always"):
        return "", {}

    clauses: list[str] = []
    params: dict[str, Any] = {}

    domain_in = when.get("domain_in")
    if domain_in:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM ref_tags awrt JOIN tags awt "
            "ON awt.tag_id = awrt.tag_id "
            f"WHERE awrt.ref_id = {ref_col} AND awt.namespace = 'DOMAIN' "
            "AND awt.value = ANY(%(domain_in)s))"
        )
        params["domain_in"] = list(domain_in)

    tags_any = when.get("tags_any")
    if tags_any:
        pairs_sql = []
        for i, token in enumerate(tags_any):
            ns, _, val = token.partition(":")
            params[f"tags_any_ns_{i}"] = ns
            params[f"tags_any_val_{i}"] = val
            pairs_sql.append(
                f"(awt2.namespace = %(tags_any_ns_{i})s AND "
                f"awt2.value = %(tags_any_val_{i})s)"
            )
        pred = " OR ".join(pairs_sql)
        if chunk_col is not None and axis.get("level") == "chunk":
            # Gate a chunk axis on the chunk's own tag (e.g. open-question
            # on ROLE3:own|background). v_chunk_tags_all already carries
            # (chunk_id, namespace, value) with ref-tag inheritance folded
            # in, so no join to `tags` is needed.
            clauses.append(
                "AND EXISTS (SELECT 1 FROM v_chunk_tags_all awt2 "
                f"WHERE awt2.chunk_id = {chunk_col} AND (" + pred + "))"
            )
        else:
            clauses.append(
                "AND EXISTS (SELECT 1 FROM ref_tags awrt2 JOIN tags awt2 "
                "ON awt2.tag_id = awrt2.tag_id "
                f"WHERE awrt2.ref_id = {ref_col} AND (" + pred + "))"
            )

    return "\n        ".join(clauses), params


# ── DB: claim + enrich, chunk level (mirrors classify.py) ──────────────


def _claim_chunk(
    conn: Any,
    axis_id: str,
    axis: dict[str, Any],
    *,
    version: str,
    limit: int,
    ref_ids: list[int] | None,
) -> list[dict[str, Any]]:
    ns = axis_id.upper()
    marker_ns = f"{ns}CASCADE"
    artifact = f"axis:{axis_id}-v{version}"
    kinds = axis.get("applies_to_kinds") or ["paper"]
    ref_filter = "AND c.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    prereq_sql, prereq_params = _prereq_clauses(
        axis, ref_col="c.ref_id", chunk_col="c.chunk_id"
    )
    applies_sql, applies_params = _applies_when_clauses(
        axis, ref_col="c.ref_id", chunk_col="c.chunk_id"
    )
    sql = f"""
    WITH cand AS (
      SELECT c.chunk_id, c.ref_id, c.ord, c.text, c.section_path
      FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
      WHERE r.kind = ANY(%(kinds)s) AND r.deleted_at IS NULL
        AND c.ord >= 0 AND c.chunk_kind = 'paragraph' AND length(c.text) > 120
        {ref_filter}
        {prereq_sql}
        {applies_sql}
        AND NOT EXISTS (SELECT 1 FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id
                        WHERE ct.chunk_id = c.chunk_id AND t.namespace = %(marker_ns)s
                        AND t.value = %(version)s)
        AND NOT EXISTS (SELECT 1 FROM chunk_claims cl
                        WHERE cl.chunk_id = c.chunk_id AND cl.artifact = %(art)s)
      ORDER BY c.chunk_id LIMIT %(limit)s
      FOR UPDATE OF c SKIP LOCKED
    ), leased AS (
      INSERT INTO chunk_claims (chunk_id, artifact)
      SELECT chunk_id, %(art)s FROM cand ON CONFLICT DO NOTHING
    )
    SELECT chunk_id, ref_id, ord, text, section_path FROM cand
    """
    params: dict[str, Any] = {
        "kinds": kinds,
        "marker_ns": marker_ns,
        "version": version,
        "art": artifact,
        "limit": limit,
        **applies_params,
        **prereq_params,
    }
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "chunk_id": r[0],
            "ref_id": r[1],
            "ord": r[2],
            "text": r[3],
            "section_path": list(r[4] or []),
        }
        for r in rows
    ]


def _enrich_chunk(conn: Any, rows: list[dict[str, Any]]) -> None:
    """Mirrors ``classify.py``'s ``_enrich`` (title/position/neighbor gists)."""
    for row in rows:
        ref_id, ord_ = row["ref_id"], row["ord"]
        meta = conn.execute(
            "SELECT r.title, (SELECT count(*) FROM chunks c2 "
            "WHERE c2.ref_id=r.ref_id AND c2.ord>=0) FROM refs r WHERE r.ref_id=%s",
            (ref_id,),
        ).fetchone()
        row["title"] = meta[0] if meta else ""
        row["position"] = f"{ord_}/{meta[1] if meta else 0}"
        row["section_path"] = " ▸ ".join(row["section_path"]) or "(none)"
        neigh: dict[int, str] = {}
        for nord in (ord_ - 1, ord_ + 1):
            if nord < 0:
                continue
            nr = conn.execute(
                "SELECT c.text, (SELECT s.text FROM chunk_summaries s "
                "WHERE s.chunk_id=c.chunk_id AND s.summarizer='llm-v1' "
                "AND s.status='ok' LIMIT 1) FROM chunks c "
                "WHERE c.ref_id=%s AND c.ord=%s LIMIT 1",
                (ref_id, nord),
            ).fetchone()
            if nr:
                neigh[nord] = (nr[1] or nr[0] or "").strip().replace("\n", " ")[:160]
        row["prev_gist"] = neigh.get(ord_ - 1, "")
        row["next_gist"] = neigh.get(ord_ + 1, "")


# ── DB: claim + enrich, ref level (mirrors classify_topics.py) ─────────


def _claim_ref(
    conn: Any,
    axis_id: str,
    axis: dict[str, Any],
    *,
    version: str,
    limit: int,
    ref_ids: list[int] | None,
) -> list[dict[str, Any]]:
    ns = axis_id.upper()
    marker_ns = f"{ns}CASCADE"
    kinds = axis.get("applies_to_kinds") or ["paper"]
    ref_filter = "AND r.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    prereq_sql, prereq_params = _prereq_clauses(
        axis, ref_col="r.ref_id", chunk_col=None
    )
    applies_sql, applies_params = _applies_when_clauses(axis, ref_col="r.ref_id")
    sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        WHERE r.kind = ANY(%(kinds)s) AND r.deleted_at IS NULL
          {ref_filter}
          {prereq_sql}
          {applies_sql}
          AND EXISTS (
            SELECT 1 FROM chunks c
            WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
          )
          AND NOT EXISTS (
            SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
            WHERE rt.ref_id = r.ref_id AND t.namespace = %(marker_ns)s AND t.value = %(version)s
          )
          {ref_lease.exclude_clause("r.ref_id", "attempt_ns")}
          -- An axis pass backfills unclassified refs — it must never claim
          -- a ref that already carries a tag in its OWN output namespace
          -- written by someone else (no cascade marker at all, any
          -- version): mint_hub (taproot/hub.py) writes TAPROOT:claim with
          -- no TAPROOTCASCADE marker, and this pass claimed + silently
          -- demoted it to TAPROOT:review seconds later via replace_prefix
          -- (2026-08-04 incident, confirmed 12x on prod) before the
          -- classifier could ever have run. A ref this axis previously
          -- classified itself always carries BOTH tags together (written
          -- in the same transaction below), so an ns-tag-with-no-marker
          -- combination can only mean a foreign writer — a version bump
          -- (ns tag + a stale-version marker) stays reclaimable as before.
          AND NOT (
            EXISTS (
              SELECT 1 FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
              WHERE rt2.ref_id = r.ref_id AND t2.namespace = %(ns)s
            )
            AND NOT EXISTS (
              SELECT 1 FROM ref_tags rt3 JOIN tags t3 ON t3.tag_id = rt3.tag_id
              WHERE rt3.ref_id = r.ref_id AND t3.namespace = %(marker_ns)s
            )
          )
        ORDER BY r.ref_id
        LIMIT %(limit)s
    """
    params: dict[str, Any] = {
        "kinds": kinds,
        "ns": ns,
        "marker_ns": marker_ns,
        "version": version,
        "attempt_ns": ref_lease.attempt_ns(marker_ns),
        "limit": limit,
        **applies_params,
        **prereq_params,
    }
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, params).fetchall()
    return [{"ref_id": int(r[0]), "title": str(r[1] or "")} for r in rows]


def _abstract(conn: Any, ref_id: int) -> str:
    """Mirrors ``classify_topics.py``'s ``_abstract`` (card_abstract, else
    the first body chunk)."""
    row = conn.execute(
        "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_abstract' "
        "AND retired_at IS NULL LIMIT 1",
        (ref_id,),
    ).fetchone()
    text = (row[0] if row else "") or ""
    if not text:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord LIMIT 1",
            (ref_id,),
        ).fetchone()
        text = (row[0] if row else "") or ""
    return text


def _enrich_ref(conn: Any, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["abstract"] = _abstract(conn, row["ref_id"])


# ── the pass ─────────────────────────────────────────────────────────────


def run_axis_pass(
    store: Any,
    *,
    dispatch: Any,
    axis_id: str,
    batch_size: int = 16,
    version: str | None = None,
    ref_ids: list[int] | None = None,
) -> dict[str, Any]:
    """One claim -> classify -> write cycle for ``data/axes/<axis_id>.yaml``.

    Returns ``{claimed, ok, failed, dist}`` (``dist`` omitted when nothing
    was claimed, matching ``classify``/``classify_topics``). ``version``
    defaults to the axis YAML's own ``version:``; passing an explicit
    override lets a caller force a re-claim without editing the file.
    ``ref_ids`` optionally restricts the claim to specific refs (targeted
    backfill / tests, mirroring the two existing passes); ``None`` sweeps
    the whole corpus.
    """
    axis = _load_axis(axis_id)
    ns = axis_id.upper()
    marker_ns = f"{ns}CASCADE"
    ver = str(version if version is not None else axis.get("version", 1))
    level = axis.get("level", "ref")
    values = set(axis.get("values") or [])
    default_unknown = axis.get("default_unknown")

    with store.pool.connection() as conn:
        if level == "chunk":
            rows = _claim_chunk(
                conn, axis_id, axis, version=ver, limit=batch_size, ref_ids=ref_ids
            )
            _enrich_chunk(conn, rows)
        else:
            rows = _claim_ref(
                conn, axis_id, axis, version=ver, limit=batch_size, ref_ids=ref_ids
            )
            _enrich_ref(conn, rows)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    dist: Counter[str] = Counter()
    for row in rows:
        prompt = (
            _build_chunk_prompt(axis, row)
            if level == "chunk"
            else _build_ref_prompt(axis, row)
        )
        if level != "chunk":
            # Ref-level claim-time attempt lease, committed BEFORE the LLM
            # call — chunk-level rows are already braked via the
            # chunk_claims lease taken at claim time (see module docstring);
            # a ref-level row has no such table, so a dispatch failure here
            # would otherwise re-claim + re-bill it every sweep (OPEN-ITEMS
            # "Unbraked LLM-pass cluster").
            with store.pool.connection() as lease_conn:
                ref_lease.stamp_attempt(
                    store, row["ref_id"], marker_ns, conn=lease_conn
                )
                lease_conn.commit()
        raw = _classify_one(dispatch, axis, prompt)
        if raw is None:
            failed += 1
            continue
        val = raw if raw in values else default_unknown
        if val is None or val not in values:
            failed += 1
            continue

        dist[val] += 1
        pos = row["ord"] if level == "chunk" else None
        with store.pool.connection() as conn:
            store.add_tag(
                row["ref_id"],
                Tag.closed(ns, val),
                pos=pos,
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
            store.add_tag(
                row["ref_id"],
                Tag.closed(marker_ns, ver),
                pos=pos,
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
            if level != "chunk":
                # Success — clear the attempt lease so an unrelated
                # re-trigger (e.g. an explicit version= override) is never
                # blocked by a stale lease from an earlier successful run.
                ref_lease.clear_attempt(store, row["ref_id"], marker_ns, conn=conn)
            conn.commit()
        ok += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed, "dist": dict(dist)}
