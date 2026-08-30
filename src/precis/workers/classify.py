"""classify — chunk-axis classifier pass (the controlled-tagging cascade).

**Why a closed faceted vocabulary, not folksonomy.** Facet tags only
buy retrieval precision when the values are few, curated, and applied
by one consistent machine tagger; free minting drifted in prod (the
open tag namespace grew ~2,700 distinct values, half used exactly
once, with facet mixing like ``interest:`` vs ``topic:`` for the same
concept). So axis vocabularies are closed YAML defs, a value edit is
a deliberate curation step, and this pass is the sole writer of its
namespace.

Self-contained ref-pass (shaped like ``llm_summarize``, not a
``WorkerHandler`` subclass: it needs DB JOINs + an outbound LLM call).
For each claimed paper body chunk it runs the **cascade**:

  1. ``junk`` gate — cheap binary; furniture short-circuits to
     ``ROLE3:furniture`` without a second call,
  2. ``role3`` — own / background / furniture, the distinction
     citation-grounding needs,
  3. (optional) escalate ``own`` chunks to a stronger model.

Tier 0 is free and structural: the claim query only admits body
``chunk_kind='paragraph'`` chunks longer than 120 chars, so furniture-by-
construction (headers, captions, references) never costs a model call.

It writes one chunk tag ``ROLE3:<value>`` via ``store.add_tag(...,
pos=ord)`` and leases each chunk in the shared ``chunk_claims`` table
under artifact ``classify:cascade-v<version>`` (bump ``CLASSIFY_VERSION``
to re-tag the corpus). Idempotent: the claim excludes chunks already
carrying a ``ROLE3`` tag. ``run_classify_pass(ref_ids=)`` scopes the sweep
to named papers — ``precis classify role3 --cites-of <draft> | --topic
<slug> | --ref-ids <csv>`` drives a targeted single-dossier backfill.

**Why a cascade.** The free local model (``summarizer`` alias) is ~72% on
the 11-way ``role`` axis — it fails the attribution test (own-work vs
others') — but 94% at junk and 88% / 91%-own-precision at the 3-way
``role3`` collapse. Human agreement is ~89%, so ~85-90% is the ceiling;
the residual is real ambiguity, absorbed by gold ``accept:`` sets and the
query-time agent. So the cheap model does the coarse high-value calls and
a stronger model is reserved for the narrow residual. ``ROLE3:own`` is
the citation-grounding filter: use it as candidate-gen / soft boost and
verify with the agent — never as a lone hard precision gate.

Axis defs (id + values + prompt + few-shot + ``applies_when``) live in
``src/precis/data/axes/*.yaml``; gold sets, eval harness, and accuracy in
``scripts/classify/`` (``gold_set/``, ``eval-classifier``,
``EVAL_RESULTS.md``; ``scripts/classify/classify --cascade`` is the
manual dry-run/commit backfill). Full design:
``chunk-classifier-cascade`` (git-only).

Default-OFF: registered unconditionally but gated per-cycle by its
``service_config`` row (``PRECIS_CLASSIFY_ENABLED`` only seeds the
deploy-time row; ``--only classify`` forces it) — a 1.3M-chunk backfill
is a deliberate, node-targeted batch.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from precis.store.types import Tag
from precis.utils.llm.json_reply import extract_json_object

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

CLASSIFY_VERSION = "1"
OUTPUT_NAMESPACE = "ROLE3"
ARTIFACT = f"classify:cascade-v{CLASSIFY_VERSION}"
_AXES_DIR = Path(__file__).resolve().parent.parent / "data" / "axes"
_ROLE3_VALS = {"own", "background", "furniture"}

#: Env var hard-capping the effective in-pass concurrency (``run_classify_pass``'s
#: ``concurrency=``) regardless of what a ``service_config`` row / caller asks
#: for — guards a fat-fingered ``/categorizers`` value (or a bad caller) from
#: stampeding the cloud endpoint / tripping the budget breaker. Clamped inside
#: :func:`run_classify_pass` itself, not just the UI that writes the knob.
MAX_CONCURRENCY_ENV = "PRECIS_CLASSIFY_MAX_CONCURRENCY"
_DEFAULT_MAX_CONCURRENCY = 32


def _max_concurrency() -> int:
    raw = os.environ.get(MAX_CONCURRENCY_ENV)
    if not raw:
        return _DEFAULT_MAX_CONCURRENCY
    try:
        val = int(raw)
    except ValueError:
        return _DEFAULT_MAX_CONCURRENCY
    return val if val >= 1 else _DEFAULT_MAX_CONCURRENCY


def _load_axis(axis_id: str) -> dict:
    return yaml.safe_load((_AXES_DIR / f"{axis_id}.yaml").read_text(encoding="utf-8"))


def _render_examples(axis: dict) -> str:
    ex = axis.get("examples") or []
    if not ex:
        return ""
    out = ["Worked examples (learn the boundaries):"]
    for e in ex:
        why = f"   # {e['why']}" if e.get("why") else ""
        out.append(f'- "{e["text"]}" -> {{"value": "{e["value"]}"}}{why}')
    return "\n".join(out) + "\n"


def _build_prompt(axis: dict, row: dict) -> str:
    """Chunk context packet declared by the axis `context:` field."""
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


_SYS = (
    "You are a precise single-label classifier. Reply with ONLY the "
    "requested JSON object, no prose."
)


def _classify_one(client: Any, axis: dict, row: dict) -> str | None:
    try:
        out = client.complete(
            [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": _build_prompt(axis, row)},
            ]
        )
    except Exception as exc:
        # A dispatch/provider failure (breaker refusal, admission block, a dead
        # or mis-configured endpoint, a transport error) is invisible in
        # llm_call_log — the router only records a call once a provider actually
        # ran (router.py:1500-1503). Swallowing it silently turns a
        # broken-endpoint window into a bare `failed` count with no forensic
        # trail, which is exactly what cost hours on gripe #172740. Surface it
        # at WARNING (transient/refused, not a crash) so the window is greppable.
        log.warning(
            "classify axis=%s chunk=%s dispatch failed: %r",
            axis.get("id") or "?",
            row.get("chunk_id"),
            exc,
        )
        return None
    return (extract_json_object(out.text) or {}).get("value")


def _classify_row(
    client: Any,
    junk_axis: dict,
    role3_axis: dict,
    escalate_client: Any | None,
    row: dict,
) -> str | None:
    """The per-chunk cascade — junk gate -> role3 -> optional Tier-2
    escalate — lifted verbatim out of :func:`run_classify_pass`'s old serial
    loop so it can be fanned out across a thread pool (each call only talks
    to ``client``/``escalate_client``, never the DB — see that function's
    docstring for the thread-safety argument). Returns the resolved value
    (a member of :data:`_ROLE3_VALS` on success) or whatever
    :func:`_classify_one` returned on a dispatch failure (``None``)."""
    if _classify_one(client, junk_axis, row) == "junk":
        return "furniture"
    val = _classify_one(client, role3_axis, row)
    if val == "own" and escalate_client is not None:
        ev = _classify_one(escalate_client, role3_axis, row)  # Tier 2 re-judge
        if ev in _ROLE3_VALS:
            val = ev
    return val


# ---- DB: claim + enrich (gold-parity context) -------------------------


def _random_chunk_anchor(conn) -> int | None:
    """A random ``chunk_id`` in ``[0, max]`` — the fair-claim scan anchor
    (Slice 3, ``docs/backlog/small-llm-derived-drain-band.md``). One cheap
    scalar (``max(chunk_id)`` is an index lookup); ``None`` on an empty corpus.
    """
    row = conn.execute(
        "SELECT floor(random() * (max(chunk_id) + 1))::bigint FROM chunks"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _claim_slice(
    conn,
    *,
    limit: int,
    ref_ids: list[int] | None,
    floor: int | None,
    floor_cmp: str,
) -> list[dict]:
    """One claim scan: chunks needing a ROLE3 tag, ``ORDER BY chunk_id`` from an
    optional ``floor`` (``floor_cmp`` = ``>=`` or ``<``), leased in the same
    statement. Kept index-friendly (a forward PK-index walk that stops at LIMIT,
    the property the ``NOT EXISTS`` predicates preserve) — the ``floor`` only
    moves the *start* of that walk, it does not force a sort."""
    ref_filter = "AND c.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    floor_filter = f"AND c.chunk_id {floor_cmp} %(floor)s" if floor is not None else ""
    sql = f"""
    WITH cand AS (
      SELECT c.chunk_id, c.ref_id, c.ord, c.text, c.section_path
      FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
      WHERE r.kind = 'paper' AND r.retired_at IS NULL
        AND c.ord >= 0 AND c.chunk_kind = 'paragraph' AND length(c.text) > 120
        {ref_filter}
        {floor_filter}
        AND NOT EXISTS (SELECT 1 FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id
                        WHERE ct.chunk_id = c.chunk_id AND t.namespace = %(ns)s)
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
    params: dict[str, Any] = {"ns": OUTPUT_NAMESPACE, "art": ARTIFACT, "limit": limit}
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    if floor is not None:
        params["floor"] = floor
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


def _claim(conn, *, limit: int, ref_ids: list[int] | None = None) -> list[dict]:
    """Lease up to ``limit`` body chunks needing a ROLE3 tag.

    ``ref_ids`` restricts to specific refs (targeted backfill / tests) and keeps
    the deterministic ``chunk_id`` order — a scoped run wants reproducibility.

    A corpus sweep (``ref_ids=None``) instead starts the ``chunk_id`` scan at a
    RANDOM anchor, then tops up from below it (Slice 3 fair-claim ordering,
    ``docs/backlog/small-llm-derived-drain-band.md``). Because a paper's chunks
    are contiguous in ``chunk_id`` (ingested together), a fixed ``ORDER BY
    chunk_id`` drains one big paper to completion before touching any other —
    starving newest papers behind the whole backfill. A random per-claim anchor
    makes consecutive claims start at different papers, so coverage is uniform
    without the seqscan+sort that a naive ``ORDER BY random()`` would force on a
    1M-row candidate set. The head top-up (``chunk_id < floor``) keeps a
    high-anchor claim from returning short; the first slice's in-statement lease
    INSERT is visible to the second (same txn), so no chunk is double-claimed.

    (``llm_summarize`` deliberately does NOT do this — its ``ref_id, ord``
    contiguity is a llama.cpp prefix-cache optimization; see that module.)
    """
    if ref_ids:
        return _claim_slice(
            conn, limit=limit, ref_ids=ref_ids, floor=None, floor_cmp=">="
        )
    floor = _random_chunk_anchor(conn)
    if floor is None:  # empty corpus
        return _claim_slice(conn, limit=limit, ref_ids=None, floor=None, floor_cmp=">=")
    rows = _claim_slice(conn, limit=limit, ref_ids=None, floor=floor, floor_cmp=">=")
    if len(rows) < limit:
        rows += _claim_slice(
            conn, limit=limit - len(rows), ref_ids=None, floor=floor, floor_cmp="<"
        )
    return rows


def unclassified_chunk_count(conn) -> int:
    """Body paragraph chunks still lacking a ``ROLE3`` tag — the backlog the
    ``materialize`` SMALL band gates on (``docs/backlog/
    small-llm-derived-drain-band.md``).

    Mirrors :func:`_claim`'s eligibility (paper body ``paragraph`` chunks >120
    chars with no ``ROLE3`` tag), WITHOUT the ``chunk_claims`` lease NOT-EXISTS
    — an approximate *backlog* for a high-water threshold, not "claimable right
    now" (same contract as ``embed.unembedded_chunk_count``). Corpus-wide (no
    ``ref_ids`` scope).
    """
    row = conn.execute(
        """
        SELECT count(*)
          FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
         WHERE r.kind = 'paper' AND r.retired_at IS NULL
           AND c.ord >= 0 AND c.chunk_kind = 'paragraph' AND length(c.text) > 120
           AND NOT EXISTS (
                   SELECT 1 FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id
                    WHERE ct.chunk_id = c.chunk_id AND t.namespace = %(ns)s
               )
        """,
        {"ns": OUTPUT_NAMESPACE},
    ).fetchone()
    return int(row[0]) if row else 0


def _enrich(conn, rows: list[dict]) -> None:
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
        neigh = {}
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


# ---- the pass ---------------------------------------------------------


def run_classify_pass(
    store: Store,
    *,
    client: Any,
    batch_size: int = 16,
    escalate_client: Any | None = None,
    ref_ids: list[int] | None = None,
    concurrency: int = 1,
) -> dict:
    """One claim→classify→write cycle. Returns {claimed, ok, failed}.

    ``escalate_client`` (Tier 2, optional — ``PRECIS_CLASSIFY_ESCALATE_MODEL``)
    re-judges chunks the cheap ``client`` calls ``own`` — the
    citation-critical, error-prone class — with a stronger model. It must be a
    **distinct** client bound to the escalate model (see ``cli/worker.py``'s
    wiring); passing ``client`` itself here would silently re-run the
    identical judgment on the identical model twice, which is a no-op
    disguised as a re-judge — the env knob would gate *whether* to
    "escalate" without ever changing *which* model runs.

    ``ref_ids`` optionally restricts the claim to specific refs (targeted
    backfill / tests, mirroring ``classify_topics``); ``None`` sweeps the
    whole corpus (unchanged behaviour).

    ``concurrency`` (live via ``service_config.concurrency`` — see
    ``workers/service_config.py``) is the thread-pool width
    the per-row LLM cascade (:func:`_classify_row`) fans out across. Each
    call in the cascade is a blocking cloud round-trip and touches no DB
    connection, so it is safe to run off the main thread; the claim, the
    enrich, and every tag write stay single-threaded on the main thread
    (psycopg connections are not thread-safe). ``client``/``escalate_client``
    are shared across the pool workers — safe because ``DispatchClient``
    (``utils/llm/router.py``, the production wiring) holds no per-call
    mutable state: every field is set at construction and ``.complete()``
    only reads ``self`` before delegating to the module-level ``dispatch()``;
    this is the same client class ``llm_summarize`` already fans out this
    way. Clamped at :data:`MAX_CONCURRENCY_ENV` regardless of what's asked
    for. Default ``1`` is byte-identical to the old always-serial loop.
    """
    junk_axis = _load_axis("junk")
    role3_axis = _load_axis("role3")

    concurrency = max(1, min(concurrency, _max_concurrency()))
    # A cap wider than batch_size would otherwise starve the pool — claim
    # enough rows to keep every worker fed.
    claim_limit = max(batch_size, concurrency)

    with store.pool.connection() as conn:
        rows = _claim(conn, limit=claim_limit, ref_ids=ref_ids)
        _enrich(conn, rows)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    if concurrency <= 1:
        # Byte-identical to the pre-concurrency serial loop.
        values = [
            _classify_row(client, junk_axis, role3_axis, escalate_client, row)
            for row in rows
        ]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            values = list(
                ex.map(
                    lambda row: _classify_row(
                        client, junk_axis, role3_axis, escalate_client, row
                    ),
                    rows,
                )
            )

    ok = failed = 0
    dist: Counter = Counter()
    # Writes stay single-threaded and in claim order (ex.map preserves
    # submission order) regardless of concurrency, so this loop — and its
    # DB behaviour — is unchanged from the old serial version.
    for row, val in zip(rows, values, strict=True):
        if val not in _ROLE3_VALS:
            failed += 1
            continue
        dist[val] += 1
        with store.pool.connection() as conn:
            store.add_tag(
                row["ref_id"],
                Tag.closed(OUTPUT_NAMESPACE, val),
                pos=row["ord"],
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
            conn.commit()
        ok += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed, "dist": dict(dist)}
