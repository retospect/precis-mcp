"""``hub_tagline`` — LLM backfill of a 3-6 word human handle per claim hub.

A taproot claim hub's ``refs.title`` is the full AIDA claim sentence by
design (one sentence, one URI) — precise, and unreadable at a glance in a
list. This pass (Reto 2026-08-28) adds a pithy DB-only handle at
``refs.meta['tagline']`` — presentation metadata, never identity: it is
mutable at any publish state (the freeze ladder only freezes the claim
string / artifact bytes, not this) and is NOT promoted into the nanopub
artifact. (Promoting it later as an ``rdfs:label`` in pubinfo — the
Nanodash/registry display convention — is one triple in ``assemble``,
affecting only not-yet-signed artifacts; do that only after a batch of
generated taglines has been human-eyeballed, since past ``signed`` the
gloss ships frozen under the attesting signature.)

**Never a source (Reto, 2026-08-28).** The tagline aids *human recall
only* — it is web-render-only and must never be used as claim content by
anything downstream: never in chunks/embeddings/search matching (only
the ``finding_body`` sentence embeds), never in evidence/grounding/
gates/the artifact assertion, never fed to downstream LLM passes as
claim text, and kept OFF agent-facing MCP payloads (``get``/``search``)
— an agent reading the gloss will eventually quote the gloss.
``tests/test_finding.py::test_tagline_never_reaches_the_agent_facing_payload``
pins the search/evidence surfaces (``view='raw'`` is exempt by contract:
it dumps the whole row for inspection).

Meta contract: ``tagline`` (str), ``tagline_by`` (``'llm' | 'human'``),
``tagline_failures`` (int, pass-internal). Render surfaces (all
absent-means-today's-rendering): ``/claim`` h1 prefix, hover popover
lead, smartdraft Claims-rail chip label, ``/nanopub`` forest row — full
sentence always one hover away. Human edit door:
``POST /claim/{head}/tagline`` (``precis_web/routes/claim.py``).

Cohort: live claim hubs (``kind='finding'`` carrying unexpired
``TAPROOT:claim`` + ``STATUS:canonical`` tags — the same pair
``taproot.reword``'s ``select_reword_cohort`` matches) whose
``meta.tagline`` is still unset. Unlike ``reword``'s cohort, no
hypothesis / disputed / publish-state / rejected-memo exclusion applies
here — a tagline is presentation gloss, not a claim edit, so every live
hub (however far along review) is eligible.

Claim-and-lease shape (:func:`_claim_candidates`), same idiom as
``stub_rank``'s LLM band (``workers/stub_rank.py::_claim_band_candidates``):
one ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED) ... RETURNING``
stamps ``meta.tagline_claimed_at`` on the rows it claims, so two cluster
nodes racing this pass in the same tick don't both pay for the same hub's
call. The claim expires after :data:`_CLAIM_TTL_MIN` minutes (a crashed
mid-batch pass doesn't strand it forever); ``meta.tagline_failures``
additionally caps lifetime paid retries at :data:`_MAX_TAGLINE_FAILURES`.

Per hub, ONE BIG-tier call (:func:`propose_tagline`) proposes the
tagline — BIG, not SMALL, by Reto's ruling (2026-08-29): a tagline is
written once and read forever, so pay for compression quality up front
rather than hand-fixing a blunt backfill later. **The model is not trusted**: :func:`_validate_tagline`
re-checks it in code before any write — non-empty, single line, at most
:data:`_MAX_WORDS` words and :data:`_MAX_CHARS` chars (after stripping
surrounding quotes/backticks and any trailing ``:``/``.``), and not the
claim sentence itself (case-insensitively, whole or a verbatim prefix —
that's not a compression, it's a truncation). A rejected or failed
proposal bumps ``meta.tagline_failures`` and writes no tagline; a written
tagline is stamped ``meta.tagline_by = 'llm'`` and the pass never
overwrites a hub whose ``tagline_by`` is already ``'human'`` (the
``POST /claim/{head}/tagline`` edit door).

Registered as the ``hub_tagline`` :class:`~precis.workers.registry.
ServiceSpec` (dark, like every other taproot service — see
``workers/registry.py``); wired in ``cli/worker.py``'s ``_register``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from precis.errors import NotFound
from precis.store import Store
from precis.taproot.canon import CLAIM_HUB_PREDICATE_PARAMS
from precis.utils.llm.router import LlmRequest, Tier, route

log = logging.getLogger(__name__)

__all__ = [
    "ProposeTaglineFn",
    "propose_tagline",
    "run_hub_tagline_pass",
]

#: Default hubs claimed per pass invocation (``cli/worker.py`` passes the
#: runner's ``batch_size`` through instead; this is the entry point's own
#: default for a direct/test call).
_DEFAULT_LIMIT = 25

#: TTL (minutes) on a :func:`_claim_candidates` lease — mirrors
#: ``stub_rank``'s ``_BAND_CLAIM_TTL_MIN``: the ``meta.tagline_claimed_at``
#: stamp IS the lease, expiring so a crashed mid-batch pass doesn't strand
#: its claims forever.
_CLAIM_TTL_MIN = 10

#: Lifetime cap on paid retries per hub (``meta.tagline_failures``) —
#: mirrors ``stub_rank``'s ``_MAX_BAND_FAILURES``. Past this, a hub is
#: excluded from every future claim and stays permanently untagged.
_MAX_TAGLINE_FAILURES = 3

#: Content-rule belt (precis.workers.hub_tagline: "3-6 words" is the
#: prompted target; these are the loose code-side ceilings a compliant
#: reply always clears).
_MAX_WORDS = 8
_MAX_CHARS = 64

_QUOTE_CHARS = "\"'`“”‘’"
_TRAILING_PUNCT_RE = re.compile(r"[:.]+$")


# ── cohort + claim-and-lease ────────────────────────────────────────────

#: Live claim hubs (mirrors ``taproot.reword``'s ``_COHORT_SQL`` tag
#: clauses — see that module's docstring on why the ``rt.expires_at``
#: guard is added here on top of ``canon.claim_hub_predicate_sql``'s bare
#: EXISTS pair) missing a tagline, not human-set, under the failure cap,
#: and not currently leased by another node's in-flight claim.
_COHORT_SQL = """\
    SELECT r.ref_id, r.title, r.meta
      FROM refs r
     WHERE r.kind = 'finding'
       AND r.retired_at IS NULL
       AND EXISTS (
             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
              WHERE rt.ref_id = r.ref_id
                AND t.namespace = %(taproot_ns)s AND t.value = %(taproot_claim)s
                AND (rt.expires_at IS NULL OR rt.expires_at > now())
           )
       AND EXISTS (
             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
              WHERE rt.ref_id = r.ref_id
                AND t.namespace = %(status_ns)s AND t.value = %(status_canonical)s
                AND (rt.expires_at IS NULL OR rt.expires_at > now())
           )
       AND r.meta->>'tagline' IS NULL
       AND r.meta->>'tagline_by' IS DISTINCT FROM 'human'
       AND COALESCE((r.meta->>'tagline_failures')::int, 0) < %(max_failures)s
       AND (r.meta->>'tagline_claimed_at' IS NULL
            OR (r.meta->>'tagline_claimed_at')::timestamptz
                 < now() - make_interval(mins => %(ttl_min)s))
     ORDER BY r.ref_id
     LIMIT %(limit)s
       FOR UPDATE OF r SKIP LOCKED
"""


def _claim_candidates(
    store: Store, *, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Atomically claim up to ``limit`` still-untagged hubs for one paid
    call each: ``(ref_id, claim sentence, meta)``.

    Same lease shape as ``stub_rank``'s ``_claim_band_candidates`` — the
    ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED) ... RETURNING``
    stamps ``meta.tagline_claimed_at`` on the claimed rows, atomically,
    so a paid call is never double-issued for the same hub within the
    lease TTL. ``RETURNING`` row order isn't guaranteed to match the
    inner ``ORDER BY``, so the claimed rows are re-sorted by ``ref_id``
    before being handed back (a stable, testable processing order).
    """
    if limit <= 0:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            UPDATE refs r
               SET meta = r.meta || jsonb_build_object(
                             'tagline_claimed_at', now()::text)
              FROM ({_COHORT_SQL}) c
             WHERE r.ref_id = c.ref_id
             RETURNING r.ref_id, c.title, c.meta
            """,
            {
                **CLAIM_HUB_PREDICATE_PARAMS,
                "max_failures": _MAX_TAGLINE_FAILURES,
                "ttl_min": _CLAIM_TTL_MIN,
                "limit": limit,
            },
        ).fetchall()
    claimed = [(int(r[0]), str(r[1] or ""), dict(r[2] or {})) for r in rows]
    claimed.sort(key=lambda c: c[0])
    return claimed


# ── the LLM call ─────────────────────────────────────────────────────────

_PROMPT_TAGLINE = """\
You are writing a short, memorable handle for a stored scientific claim
sentence -- a "tagline" a human skims before reading the full sentence.

CLAIM SENTENCE:
{sentence}

SCOPE (structured context; may be empty):
{scope_json}

Write a 3-6 word tagline that compresses the claim into a punchy, human
handle:
  - Sentence-cased, no trailing period.
  - No new facts -- a compression of the claim, not a second claim.
  - May be informal/punchy (e.g. "Graphene is FET").
  - Do NOT just repeat (or truncate a prefix of) the claim sentence
    verbatim -- that is not a compression.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "tagline": "<3-6 word tagline>"
}}
"""

#: The injectable LLM seam — ``(sentence, scope)`` to the parsed JSON
#: dict, or ``None`` on failure (the ``_chase_llm`` / ``reword`` contract).
ProposeTaglineFn = Callable[[str, dict[str, Any]], "dict[str, Any] | None"]


def propose_tagline(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
    """One BIG-tier tagline proposal. Returns the parsed JSON dict or
    ``None`` on dispatch failure — the caller counts it as a failed
    attempt and moves on; a model that never ran is never a verdict."""
    prompt = _PROMPT_TAGLINE.format(
        sentence=sentence, scope_json=json.dumps(scope, sort_keys=True)
    )
    res = route(LlmRequest(tier=Tier.BIG, prompt=prompt, source="hub_tagline"))
    if res.error:
        log.warning("hub_tagline: propose hook failed: %s", res.error)
        return None
    return res.data


# ── post-validation (belt over the LLM) ─────────────────────────────────


def _strip_quotes(text: str) -> str:
    """Strip one layer of matching surrounding quote/backtick chars."""
    if len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        return text[1:-1].strip()
    return text


def _clean_tagline(raw: str) -> str:
    """Strip surrounding quotes/backticks and any trailing ``:``/``.``,
    repeatedly to a fixpoint (handles ``"tagline."`` and ``"tagline".``
    alike)."""
    text = raw.strip()
    while True:
        cleaned = _TRAILING_PUNCT_RE.sub("", _strip_quotes(text)).strip()
        if cleaned == text:
            return cleaned
        text = cleaned


def _validate_tagline(raw: Any, sentence: str) -> str | None:
    """The proposal, cleaned and validated, or ``None`` on any belt
    failure. Never raises — a non-string/empty/multi-line/over-long/
    over-worded proposal, or one that (case-insensitively) equals the
    claim sentence or is a verbatim prefix of it, is rejected outright."""
    if not isinstance(raw, str):
        return None
    if "\n" in raw or "\r" in raw:
        return None
    tagline = _clean_tagline(raw)
    if not tagline:
        return None
    if len(tagline) > _MAX_CHARS:
        return None
    if len(tagline.split()) > _MAX_WORDS:
        return None
    sentence_norm = sentence.strip().casefold()
    tagline_norm = tagline.casefold()
    if tagline_norm == sentence_norm:
        return None
    if sentence_norm.startswith(tagline_norm):
        return None
    return tagline


# ── the pass ─────────────────────────────────────────────────────────────


def _run_llm(
    store: Store, *, limit: int, propose_fn: ProposeTaglineFn
) -> tuple[int, int]:
    """Claim up to ``limit`` hubs, propose + validate + write per hub.
    Returns ``(attempted, ok)``. Never raises on a single hub's LLM
    failure or a rejected proposal — both bump ``meta.tagline_failures``
    (via ``store.update_ref``'s shallow merge, the same idiom the write
    path uses) and clear the claim stamp so a later pass can retry (up to
    the lifetime cap)."""
    candidates = _claim_candidates(store, limit=limit)
    if not candidates:
        return 0, 0

    ok = 0
    for ref_id, sentence, meta in candidates:
        scope = {str(k): str(v) for k, v in (meta.get("scope") or {}).items()}
        try:
            data = propose_fn(sentence, scope)
        except Exception:
            log.warning(
                "hub_tagline: propose hook raised for hub %d", ref_id, exc_info=True
            )
            data = None

        raw = data.get("tagline") if isinstance(data, dict) else None
        tagline = _validate_tagline(raw, sentence)
        try:
            if tagline is None:
                failures = int(meta.get("tagline_failures") or 0) + 1
                store.update_ref(
                    ref_id,
                    meta_patch={
                        "tagline_failures": failures,
                        "tagline_claimed_at": None,
                    },
                )
                continue

            store.update_ref(
                ref_id,
                meta_patch={
                    "tagline": tagline,
                    "tagline_by": "llm",
                    "tagline_claimed_at": None,
                },
            )
        except NotFound:
            # Deleted between claim and write — skip it, keep the batch;
            # the lease TTL frees any state the row left behind.
            log.info("hub_tagline: hub %d vanished mid-batch, skipping", ref_id)
            continue
        ok += 1

    return len(candidates), ok


def run_hub_tagline_pass(
    store: Store,
    *,
    limit: int = _DEFAULT_LIMIT,
    propose_fn: ProposeTaglineFn | None = None,
) -> dict[str, int]:
    """Run one ``hub_tagline`` pass: claim up to ``limit`` untagged live
    claim hubs, propose + validate + write a tagline for each.

    ``propose_fn`` is the injectable LLM seam for tests (default
    :func:`propose_tagline`). Returns the ``BatchResult`` shape
    ``{claimed, ok, failed}`` — ``claimed`` is every hub attempted this
    pass, ``ok`` the ones that landed a written tagline; a rejected
    proposal or a dead LLM call counts toward ``claimed``/``failed`` only,
    never raising the pass.
    """
    fn = propose_fn or propose_tagline
    attempted, ok = _run_llm(store, limit=limit, propose_fn=fn)
    return {"claimed": attempted, "ok": ok, "failed": attempted - ok}
