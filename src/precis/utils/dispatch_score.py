"""``dispatch_score`` — the machine-owned sequencing score (gr604593).

``refs.prio`` carries two jobs today: a human's judgement of importance
and the queue's ordering number, which ``gripe_triage`` overwrote every
tick with a banded score placed *between* the named rungs so machine and
human values interleaved on one column. Ruling (Reto 2026-11-09): split
them. ``prio`` is human- or filer-set only, sparse, named-rung valued
(``PRIO:urgent`` 1 / ``high`` 3 / ``normal`` 5 / ``low`` 8); the queue
order is a score nobody hand-tunes, computed from the signals the bands
hid behind plus the sequencing signals that lived outside ``prio``
already.

The score (higher = hotter) is:

    COALESCE(severity-path, meta.dispatch_score, UNSCORED_BASE)
      × prio_multiplier(prio)        — a set ``prio`` is a multiplier,
                                       never the sort key itself
      × SHOWSTOPPER_MULT             — a ``showstopper`` tag heats it
      × FACTORY_MULT                 — ``cluster:dark-factory`` /
                                       ``thread:factory`` heats it
                                       (Reto 2026-11-12: factory-
                                       throughput gripes ×3, below the
                                       release class score of 32)

* **severity-path** — the triage tick's judgement, recomputed live so
  age and parked dups never go stale:
  ``meta.dispatch.severity × (1 + supersedes-linked dups) ×
  min(1 + age_weeks, AGE_FACTOR_CAP)``. The age factor saturates — the
  dry-run ruling (2026-10-25) ported from band space (third-party: n/a —
  a ruling, not vendored code): an old papercut
  must not climb on age alone.
* **``meta.dispatch_score``** — a machine-stamped value for refs the
  severity path can't cover: auto-filed gripes
  (:mod:`precis.workers.auto_gripe`), the migration's bridge for
  converted machine prios, and plan nodes (``mint_gripe_fix`` stamps the
  gripe's live score on the chain root — "plan roots inherit the
  score"; ``pr_class`` and ``mint_release_pass`` stamp class scores).
* **Holds stay hard.** ``blocked-by``/``waiting-for:``/``hold:*`` still
  exclude a candidate outright — the score only orders what the holds
  let through — and a slice whose brief file set overlaps an in-flight
  land scores below it (:data:`OVERLAP_DEMOTE`, td603849) until that
  land finishes, not behind a pseudo-prio nobody remembers setting.

:func:`score_sql` emits the SQL fragment both ``gripe_dispatch``'s
candidate query and ``gripe_order``'s ``drain`` sort interpolate — one
shape, so the queue the web shows is the queue the lane drains. The
fragment is static text; callers never build it from input.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

#: ``meta`` key a machine stamps a finished score under (auto-filed
#: gripes, plan nodes, the 0294 conversion of machine-written prios).
SCORE_META_KEY = "dispatch_score"
#: ``meta`` key holding the triage tick's score inputs
#: (``{"severity": 1..5}``); the live score recomputes over it.
DISPATCH_META_KEY = "dispatch"

#: Score a gripe with no triage severity and no stamped score sorts at —
#: below any real judgement but never NULL, so the unjudged pile drains
#: oldest-first instead of sitting behind everything.
UNSCORED_BASE = 1.0
#: Base score a plan node with no stamped score on it or any ancestor
#: takes (``plan_dispatch``'s old ``DEFAULT_PRIO`` carried over — a
#: minted plan is sanctioned work, mid-queue).
DEFAULT_SCORE = 5.0
#: ``mint_release_pass`` stamps this on the plan root — the
#: release-fixer class outranks routine builds the way a hand-set
#: prio-1 release fixer used to.
RELEASE_SCORE = 32.0
#: ``meta`` key a plan root and its jobs carry to claim in a named band,
#: and the one band it names: ``release`` ranks the job at the release
#: legs' class (2.5, ``executors/_common.py``), ahead of ordinary gripe
#: builds. ``plan_dispatch._mint_job`` copies the root's stamp onto the
#: build/land jobs; the waiver gripe's chain (``red_waiver``) carries it.
CLAIM_BAND_META_KEY = "claim_band"
RELEASE_CLAIM_BAND = "release"
#: Age's contribution to the severity path saturates here (the band-era
#: "at most one band on age alone" ruling, made continuous).
AGE_FACTOR_CAP = 4.0
#: A ``showstopper`` tag multiplies the score by this — a stopped lane's
#: work drains hot.
SHOWSTOPPER_MULT = 2.0
#: Open tags that mark a gripe as dark-factory throughput work.
FACTORY_TAGS = ("cluster:dark-factory", "thread:factory")
#: A :data:`FACTORY_TAGS` tag multiplies the score by this — gripes that
#: speed up the factory drain first (Reto 2026-11-12). Stays under the
#: release class (:data:`RELEASE_SCORE`).
FACTORY_MULT = 3.0
#: A candidate whose brief file set overlaps an in-flight land's keeps
#: this fraction of its score until that land finishes (td603849).
OVERLAP_DEMOTE = 0.01
#: ``recommend: PRIO:<rung>`` fires at these floors; between them no
#: rung is worth a human nudge.
RECOMMEND_URGENT = 16.0
RECOMMEND_HIGH = 8.0


def prio_multiplier(prio: int | None) -> float:
    """The multiplier a human-set ``refs.prio`` applies to a score.

    ``2 ** ((5 - prio) / 2)``: urgent (1) ×4, high (3) ×2, normal (5)
    ×1, low (8) ≈×0.35 — every column value lands on the same monotone
    curve, so an off-ladder hand-set still moves the score the right
    way. Unset multiplies by 1.
    """
    if prio is None:
        return 1.0
    return float(2.0 ** ((5 - int(prio)) / 2.0))


def gripe_score(
    severity: int | None,
    dups: int,
    age_days: float,
    *,
    prio: int | None = None,
    showstopper: bool = False,
    factory: bool = False,
    stamped: float | None = None,
) -> float:
    """The Python twin of :func:`score_sql` — used where a caller holds
    the inputs rather than a row (triage's recommend comment, tests)."""
    if isinstance(severity, int) and severity > 0:
        base = (
            severity
            * (1 + max(0, dups))
            * min(1 + max(0.0, age_days) / 7.0, AGE_FACTOR_CAP)
        )
    elif stamped is not None:
        base = float(stamped)
    else:
        base = UNSCORED_BASE
    score = base * prio_multiplier(prio)
    if showstopper:
        score *= SHOWSTOPPER_MULT
    if factory:
        score *= FACTORY_MULT
    return score


def recommended_rung(score: float) -> str | None:
    """The rung a triage tick may nudge a human toward in a
    ``recommend: PRIO:<rung>`` comment — ``urgent``/``high`` only; a
    middling score is no judgement worth nudging over."""
    if score >= RECOMMEND_URGENT:
        return "urgent"
    if score >= RECOMMEND_HIGH:
        return "high"
    return None


def age_days(created_at: datetime, now: datetime | None = None) -> float:
    """Age in days for :func:`gripe_score` (naive ``created_at`` reads UTC)."""
    now = now or datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return max(0.0, (now - created_at).total_seconds() / 86400.0)


# ── SQL ────────────────────────────────────────────────────────────────

_FACTORY_TAGS_SQL = ", ".join(f"'{t}'" for t in FACTORY_TAGS)

#: ``refs r`` fragment — every consumer interpolates the SAME expression
#: so the queue can't fork across surfaces. Correlated subqueries match
#: the shapes ``gripe_order`` already runs per row.
_SCORE_SQL = f"""\
COALESCE(
  CASE WHEN r.meta->'{DISPATCH_META_KEY}'->>'severity' ~ '^[0-9]+$'
    THEN (r.meta->'{DISPATCH_META_KEY}'->>'severity')::float
         * (1 + (SELECT count(*) FROM links l
                  WHERE l.src_ref_id = r.ref_id
                    AND l.relation = 'supersedes'))
         * LEAST(1 + EXTRACT(EPOCH FROM now() - r.created_at) / 604800.0,
                 {AGE_FACTOR_CAP})
  END,
  (r.meta->>'{SCORE_META_KEY}')::float,
  {UNSCORED_BASE}
) * CASE WHEN r.prio IS NULL THEN 1.0
         ELSE power(2.0, (5 - r.prio) / 2.0) END
  * CASE WHEN EXISTS (SELECT 1 FROM ref_tags xrt JOIN tags xt
                                ON xt.tag_id = xrt.tag_id
                       WHERE xrt.ref_id = r.ref_id
                         AND xt.namespace IN ('OPEN', 'FLAG')
                         AND xt.value = 'showstopper')
          THEN {SHOWSTOPPER_MULT} ELSE 1.0 END
  * CASE WHEN EXISTS (SELECT 1 FROM ref_tags frt JOIN tags ft
                                ON ft.tag_id = frt.tag_id
                       WHERE frt.ref_id = r.ref_id
                         AND ft.namespace = 'OPEN'
                         AND ft.value IN ({_FACTORY_TAGS_SQL}))
          THEN {FACTORY_MULT} ELSE 1.0 END"""


def score_sql() -> str:
    """The live ``dispatch_score`` expression over ``refs r`` (higher =
    hotter; never NULL — an unjudged gripe scores
    :data:`UNSCORED_BASE`)."""
    return _SCORE_SQL


# ── brief file sets (the td603849 overlap hold) ─────────────────────────

#: A repo-path token a brief names — ``src/precis/workers/gripe_triage.py``,
#: ``workers/gripe_triage.py::prio_for``, ``tests/test_foo.py``, a bare
#: ``land_slice.py``. Extensions cover the buildable surfaces.
_FILE_RE = re.compile(
    r"[A-Za-z0-9_./-]*[A-Za-z0-9_-]+"
    r"\.(?:py|pyi|sql|sh|bash|yml|yaml|toml|json|j2|ts|tsx|js|jsx|css|html|"
    r"md|rs|go|mjs|c|h|cc|cpp|tex)"
)
_ANCHOR_RE = re.compile(r"::.*$")
_LEADING_ROOTS = ("./", "src/precis/", "src/")


def brief_files(text: str) -> frozenset[str]:
    """The file set a brief or gripe report names, normalised for
    overlap comparison.

    Every token yields up to three keys — the full normalised path, its
    last two segments, and its basename — so ``src/precis/x/y.py``,
    ``x/y.py`` and ``y.py`` all meet on a shared key. Over-matching is
    the safe direction: a false overlap only delays a candidate behind
    an in-flight land; a missed one rebuilds the td603849 land
    conflict."""
    keys: set[str] = set()
    for raw in _FILE_RE.findall(text or ""):
        path = _ANCHOR_RE.sub("", raw).strip("./")
        for root in _LEADING_ROOTS:
            if path.startswith(root):
                path = path[len(root) :]
                break
        if not path:
            continue
        keys.add(path)
        parts = path.split("/")
        if len(parts) >= 2:
            keys.add("/".join(parts[-2:]))
        keys.add(parts[-1])
    return frozenset(keys)


__all__ = [
    "AGE_FACTOR_CAP",
    "CLAIM_BAND_META_KEY",
    "DEFAULT_SCORE",
    "DISPATCH_META_KEY",
    "FACTORY_MULT",
    "FACTORY_TAGS",
    "OVERLAP_DEMOTE",
    "RELEASE_CLAIM_BAND",
    "RELEASE_SCORE",
    "SCORE_META_KEY",
    "SHOWSTOPPER_MULT",
    "UNSCORED_BASE",
    "age_days",
    "brief_files",
    "gripe_score",
    "prio_multiplier",
    "recommended_rung",
    "score_sql",
]
