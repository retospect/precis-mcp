"""Categorizer inventory, live toggle state and coverage counts — shared reads.

The closed-vocabulary tagging layer unifies two families:

  * **Axes** (``src/precis/data/axes/*.yaml``) — one closed-vocabulary
    categorizer per file (schema: ``axes/README.md``). A chunk-level axis
    (``level: chunk``) writes ``<ID>:<value>`` chunk tags; a ref-level axis
    (``level: ref`` — the default when the field is omitted) writes a ref tag
    in the same ``<ID>``-uppercased namespace. **role3** + **junk** run under
    the ``classify`` cascade; every other axis runs under its own
    ``axis:<id>`` generic-runner service (``cli/_worker_passes.py``'s per-axis
    wiring, ``workers/axis_pass.py``) — default-OFF but independently
    flippable.
  * **Topics** (``src/precis/data/topics/*.yaml``) — paper/patent-level,
    multi-label ``topic:<slug>`` open tags plus a ``TOPICCASCADE:<marker>``
    marker. All topics share the one ``classify_topics`` pass, but each topic
    is independently flippable via its own ``topic:<slug>`` service: the pass
    filters ``load_topics()`` to the enabled subset and the marker encodes
    that enabled-topic set, so toggling a topic lazily re-sweeps the corpus
    against the new set. ``classify_topics`` itself remains a global
    kill-switch — an explicit off row there force-kills the pass regardless
    of any topic's own state.

Everything here is read-only and store-only (no web imports), so the three
surfaces share one implementation:

  * ``precis_web/routes/categorizers.py`` — the ``/categorizers`` console
    (shell + deferred ``/categorizers/progress`` fragment) and its toggles;
  * ``precis classify progress`` — the auth-free CLI coverage readout;
  * the search-side coverage gate / machine-hint line (see
    :func:`axis_filter_ready`, :func:`categorizer_filter_notes`,
    :func:`ref_hint_line`).
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from precis.utils.env import env_csv_set, env_flag
from precis.workers.service_config import (
    ALL_HOSTS,
    ServiceConfigResolver,
    list_service_config,
)

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref, Tag

log = logging.getLogger(__name__)

AXES_DIR = Path(__file__).resolve().parent / "data" / "axes"
TOPICS_DIR = Path(__file__).resolve().parent / "data" / "topics"

#: §L control cutover retired ``PRECIS_CLASSIFY_ENABLED`` as ``classify``'s
#: live default (``cli/worker.py``'s ``_profile_default_on`` — no
#: ``default_profiles``, so it's unconditionally OFF absent a row now);
#: ``classify``'s default_on in :func:`effective_state` is a literal
#: ``False``, not an env read.
#:
#: ``classify_topics`` is the one exception left in place (mirrors
#: ``precis.workers.registry`` ``ServiceSpec.enable_env`` — read directly
#: rather than importing the registry so this module stays decoupled from
#: the worker-CLI wiring): its OWN top-level kill-switch default still
#: folds in whether ANY topic would be enabled (``_gate_default_on``
#: special-cases it), which is the one narrower default this cutover
#: deliberately left alone (a granular per-topic deploy-time seed, not a
#: whole-pass boot flag).
CLASSIFY_TOPICS_ENABLED_ENV = "PRECIS_CLASSIFY_TOPICS_ENABLED"

#: Comma-separated axis ids ``cli/_worker_passes.py``'s per-axis wiring
#: seeds its ``default_on`` verdict from (a live ``service_config`` row
#: always wins).
AXES_ENABLED_ENV = "PRECIS_AXES_ENABLED"

#: Comma-separated topic slugs the per-topic gate seeds its ``default_on``
#: verdict from — mirrors :data:`AXES_ENABLED_ENV`.
TOPICS_ENABLED_ENV = "PRECIS_TOPICS_ENABLED"

#: Comma-separated opt-in ref kinds (``workers/_claim.py``
#: :data:`OPT_IN_KINDS`) ``cli/_worker_passes.py`` seeds its
#: ``categorize_kind:<kind>`` ``default_on`` verdicts from.
CATEGORIZE_KINDS_ENV = "PRECIS_CATEGORIZE_KINDS"

#: Axis ids the ``classify`` cascade actually drives. ``junk`` is a
#: gate folded into ``ROLE3:furniture`` — it never writes its own
#: ``JUNK:`` tag (see ``workers/classify.py``) — so its own coverage
#: count is always 0 even while the pass is active; that 0% is correct,
#: not a bug.
CLASSIFY_PASS_AXES = frozenset({"role3", "junk"})

TOPIC_MARKER_NAMESPACE = "TOPICCASCADE"

#: Coverage fraction at which a categorizer axis may be *advertised* as a
#: search filter (gr515633): below it, ``tags=[<NS>:<value>]`` silently
#: drops the still-unclassified share of the corpus, so search instead
#: warns (:func:`categorizer_filter_notes`). 0.95 is Reto's bar
#: (2026-10-24).
FILTER_READY_COVERAGE = 0.95


def topic_service(slug: str) -> str:
    """The per-topic ``service_config`` service name — each topic
    is independently flippable; ``classify_topics`` itself remains the
    retained global kill-switch (see :func:`allowed_services`)."""
    return f"topic:{slug}"


def current_marker_value(enabled_slugs: list[str]) -> str | None:
    """The live done-marker value for ``enabled_slugs``, imported lazily from
    ``workers/classify_topics.py`` (kept import-time-decoupled since another
    agent may be actively editing that module). ``None`` on import failure —
    the caller degrades the topic coverage rows rather than failing."""
    try:
        from precis.workers.classify_topics import topic_marker_value

        return topic_marker_value(enabled_slugs)
    except Exception:
        log.exception("categorizers: topic_marker_value import failed")
        return None


def _dir_signature(d: Path) -> tuple[tuple[str, int], ...]:
    """``(name, mtime_ns)`` per ``*.yaml`` in ``d`` — the cheap change
    token the parse cache keys on. ~30 ``stat`` calls vs ~30 YAML parses;
    a file added/edited on disk flips the signature, so a new axis or
    topic still goes live without a restart."""
    return tuple((p.name, p.stat().st_mtime_ns) for p in sorted(d.glob("*.yaml")))


_yaml_cache_lock = threading.Lock()
#: dir path -> (signature, parsed docs). Bounded by the number of yaml
#: dirs read through :func:`_load_dir` (two today).
_yaml_cache: dict[Path, tuple[tuple[tuple[str, int], ...], list[dict[str, Any]]]] = {}


def _load_dir(d: Path, id_field: str, label: str) -> list[dict[str, Any]]:
    """Every ``*.yaml`` doc in ``d`` that defines ``id_field``, cached on
    the dir signature — ``ref_hint_line`` runs on every fisheye, so the
    parse must not repeat per render."""
    sig = _dir_signature(d)
    with _yaml_cache_lock:
        hit = _yaml_cache.get(d)
        if hit is not None and hit[0] == sig:
            return hit[1]
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("categorizers: failed to parse %s file %s", label, path)
            continue
        if isinstance(data, dict) and data.get(id_field):
            out.append(data)
    with _yaml_cache_lock:
        _yaml_cache[d] = (sig, out)
    return out


def load_axes() -> list[dict[str, Any]]:
    """Every axis YAML that defines an ``id``.

    Skips ``journal_domains.yaml`` (a journal->domain lookup table, not
    itself a categorizer — it has no ``id`` field) and ``README.md``
    (non-YAML, excluded by the glob already).
    """
    return _load_dir(AXES_DIR, "id", "axis")


def load_topics() -> list[dict[str, Any]]:
    return _load_dir(TOPICS_DIR, "slug", "topic")


def governing_service(axis_id: str) -> str:
    """The ``service_config`` service that gates ``axis_id``'s runtime pass.

    ``role3``/``junk`` share the one ``classify`` cascade pass; every
    other axis runs under its own ``axis:<id>`` generic-runner service
    (``cli/_worker_passes.py``'s per-axis wiring, ``workers/axis_pass.py``).
    """
    return "classify" if axis_id in CLASSIFY_PASS_AXES else f"axis:{axis_id}"


def allowed_services() -> frozenset[str]:
    """Every ``service_config`` service name the categorizers surface may
    toggle.

    Read live off the axis/topic YAMLs (not hardcoded) so a new axis or
    topic file is toggleable without a code change, and re-derived per
    request so a concurrently-added file doesn't need a worker restart to
    become toggleable. Guards ``POST /categorizers/toggle`` against writing
    an arbitrary ``service_config`` row. ``classify_topics`` is retained as
    the global kill-switch target alongside each topic's own
    ``topic:<slug>`` service.
    """
    axis_ids = {str(a["id"]) for a in load_axes()}
    topic_slugs = {
        str(t["slug"]) for t in load_topics() if isinstance(t, dict) and t.get("slug")
    }
    return frozenset(
        {"classify", "classify_topics"}
        | {governing_service(a) for a in axis_ids if a not in CLASSIFY_PASS_AXES}
        | {topic_service(s) for s in topic_slugs}
    )


def _override_rows(store: Store) -> frozenset[str]:
    """Services carrying an explicit all-hosts (``*``) ``service_config``
    row — the "overridden" (vs "env/profile default") signal the UI
    shows alongside the effective on/off state. Degrades to "no
    overrides known" on a schema surprise (pre-migration DB) rather than
    failing the page."""
    try:
        rows = list_service_config(store)
    except Exception:
        log.exception("categorizers: list_service_config failed")
        return frozenset()
    return frozenset(str(r["service"]) for r in rows if r["host"] == ALL_HOSTS)


def effective_state(store: Store) -> dict[str, dict[str, Any]]:
    """``service -> {"enabled": bool, "overridden": bool, "concurrency": int}``
    for every service the categorizers surface governs, scoped to the
    all-hosts (``*``) row — the live ``service_config`` override wins over
    the env/profile default, mirroring ``cli/_worker_passes.py``'s
    ``_pass_enabled`` contract. ``concurrency`` defaults to 1 (serial) when
    no row/value is set; only the ``classify`` cascade (role3/junk)
    actually reads it today, but the knob is exposed uniformly per row so
    any pass can pick it up later."""
    resolver = ServiceConfigResolver(store, ALL_HOSTS)
    overridden = _override_rows(store)
    axes_env = env_csv_set(AXES_ENABLED_ENV)
    # Mirrors cli/_worker_passes.py's `_classify_topics_enabled_slugs` /
    # `_gate_default_on`: PRECIS_CLASSIFY_TOPICS_ENABLED=1 is the legacy
    # "all topics default-on" admin seed, so a per-topic row's own
    # ``default_on`` must fold it in too — otherwise the readout would show a
    # topic off that the worker actually classifies.
    global_topics_on = env_flag(CLASSIFY_TOPICS_ENABLED_ENV)

    out: dict[str, dict[str, Any]] = {
        "classify": {
            # §L: no env fallback any more — a formerly dark-switched pass with
            # no service_config row now defaults OFF (mirrors
            # cli/_worker_passes.py's ``_profile_default_on``).
            "enabled": resolver.enabled("classify", default_on=False),
            "overridden": "classify" in overridden,
            "concurrency": resolver.concurrency("classify", default=1),
        },
        # Retained as the global kill-switch state — each topic
        # now also carries its own independent ``topic:<slug>`` state below.
        "classify_topics": {
            "enabled": resolver.enabled("classify_topics", default_on=global_topics_on),
            "overridden": "classify_topics" in overridden,
            "concurrency": resolver.concurrency("classify_topics", default=1),
        },
    }
    for axis in load_axes():
        axis_id = str(axis["id"])
        if axis_id in CLASSIFY_PASS_AXES:
            continue
        service = governing_service(axis_id)
        out[service] = {
            "enabled": resolver.enabled(service, default_on=axis_id in axes_env),
            "overridden": service in overridden,
            "concurrency": resolver.concurrency(service, default=1),
        }

    topics_env = env_csv_set(TOPICS_ENABLED_ENV)
    for topic in load_topics():
        if not (isinstance(topic, dict) and topic.get("slug")):
            continue
        slug = str(topic["slug"])
        service = topic_service(slug)
        out[service] = {
            "enabled": resolver.enabled(
                service, default_on=global_topics_on or slug in topics_env
            ),
            "overridden": service in overridden,
            "concurrency": resolver.concurrency(service, default=1),
        }
    return out


def opt_in_kind_enabled(store: Store, kind: str) -> bool:
    """Live verdict for ``categorize_kind:<kind>`` — the operator opt-in that
    lets the categorizers claim ``workers/_claim.py``'s default-OFF ref kinds
    (``finding`` today). Mirrors ``cli/_worker_passes.py``'s
    ``_opt_in_kinds``: the ``PRECIS_CATEGORIZE_KINDS`` env list only seeds
    the ``default_on``; a live ``service_config`` row always wins."""
    from precis.workers._claim import opt_in_service

    resolver = ServiceConfigResolver(store, ALL_HOSTS)
    return resolver.enabled(
        opt_in_service(kind), default_on=kind in env_csv_set(CATEGORIZE_KINDS_ENV)
    )


def _safe(fn: Any) -> Any:
    """Run a query closure, returning its result or ``None`` on error.

    A schema surprise in one categorizer's count degrades that row instead
    of failing the whole readout.
    """
    try:
        return fn()
    except Exception:
        log.exception("categorizers: coverage query failed")
        return None


def chunk_eligible_total(store: Store) -> int:
    """Count of eligible body chunks — the shared ``total`` denominator for
    every chunk-level axis, mirroring the ``classify`` cascade's claim
    predicate (``workers/classify.py``'s ``_claim``): real body paragraphs
    long enough to be worth tagging. Computed once per readout (this
    scan is the same for all chunk axes), not per axis."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*)::int FROM chunks "
            "WHERE ord >= 0 AND chunk_kind = 'paragraph' AND length(text) > 120"
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def chunk_axis_progress(
    store: Store, namespace: str, eligible_total: int
) -> tuple[int, int, datetime | None]:
    """(done, total, last_ts) for a chunk-level axis's own namespace. ``done``
    = chunks already carrying a tag in this namespace; ``total`` is the
    shared eligible-chunk count passed in (see :func:`chunk_eligible_total`)
    so the expensive scan runs once, not once per axis; ``last_ts`` is the
    ``created_at`` of the most recently written tag in this namespace
    (``None`` if none), folded into this same scan at zero extra cost."""
    with store.pool.connection() as conn:
        done_row = conn.execute(
            "SELECT count(DISTINCT ct.chunk_id)::int, max(ct.created_at) "
            "FROM chunk_tags ct "
            "JOIN tags t ON t.tag_id = ct.tag_id WHERE t.namespace = %s",
            (namespace,),
        ).fetchone()
    done = int(done_row[0]) if done_row and done_row[0] is not None else 0
    last_ts = done_row[1] if done_row else None
    return done, eligible_total, last_ts


def ref_axis_progress(
    store: Store, namespace: str, paper_patent_total: int
) -> tuple[int, int, datetime | None]:
    """(done, total, last_ts) for a ref-level axis: refs tagged in its
    namespace, over the paper+patent corpus. Each non-cascade axis now has a
    runtime pass (``axis:<id>``, default-OFF) — ``done`` reads 0 until its
    toggle is flipped on and it's had a chance to sweep the corpus, not
    because no code exists. ``last_ts`` is the ``created_at`` of the most
    recently written tag in this namespace (``None`` if none), folded into
    this same scan at zero extra cost."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT rt.ref_id)::int, max(rt.created_at) "
            "FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL "
            "AND t.namespace = %(ns)s",
            {"kinds": ["paper", "patent"], "ns": namespace},
        ).fetchone()
    done = int(row[0]) if row and row[0] is not None else 0
    last_ts = row[1] if row else None
    return done, paper_patent_total, last_ts


def paper_patent_total(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*)::int FROM refs "
            "WHERE kind = ANY(%s) AND retired_at IS NULL",
            (["paper", "patent"],),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def topics_marker_done(store: Store, marker_value: str) -> int:
    """Paper+patent refs carrying the current ``TOPICCASCADE:<marker>``
    marker — the ``classify_topics`` pass's coverage of the corpus under the
    *live enabled-topic set* (per-topic classify gating — a toggle changes the marker value,
    so this recomputes against the new set; same value for every topic row;
    each topic's own hit-count is a separate, per-slug number — see
    :func:`topic_hit_count`)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT rt.ref_id)::int FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL "
            "AND t.namespace = %(ns)s AND t.value = %(marker)s",
            {
                "kinds": ["paper", "patent"],
                "ns": TOPIC_MARKER_NAMESPACE,
                "marker": marker_value,
            },
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def topic_hit_count(store: Store, slug: str) -> tuple[int, datetime | None]:
    """(hit_count, last_ts) — how many paper+patent refs carry
    ``topic:<slug>`` (an OPEN tag — ``workers/classify_topics.py`` writes
    ``Tag.open(f"topic:{slug}")``, so the DB namespace is the ``OPEN``
    sentinel, not the slug itself), plus the ``created_at`` of the most
    recently written one (``None`` if none), folded into this same scan at
    zero extra cost. Only called from :func:`progress_rows` and the
    search-side coverage note."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT rt.ref_id)::int, max(rt.created_at) "
            "FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL "
            "AND t.namespace = 'OPEN' AND t.value = %(val)s",
            {"kinds": ["paper", "patent"], "val": f"topic:{slug}"},
        ).fetchone()
    hit_count = int(row[0]) if row and row[0] is not None else 0
    last_ts = row[1] if row else None
    return hit_count, last_ts


def enabled_topic_slugs(store: Store) -> list[str]:
    """The topic slugs whose own ``topic:<slug>`` service is currently on —
    the subset ``classify_topics`` actually sweeps (and therefore the set the
    ``TOPICCASCADE`` marker value encodes)."""
    topics = load_topics()
    effective = _safe(lambda: effective_state(store)) or {}
    return [
        str(t["slug"])
        for t in topics
        if isinstance(t, dict)
        and t.get("slug")
        and (effective.get(topic_service(str(t["slug"]))) or {}).get("enabled")
    ]


def progress_rows(store: Store) -> dict[str, dict[str, Any]]:
    """One coverage row per categorizer, keyed by name. Each is computed
    independently via :func:`_safe` so one schema surprise degrades only
    that row (``error: True``) instead of the whole readout."""
    rows: dict[str, dict[str, Any]] = {}
    pp_total = _safe(lambda: paper_patent_total(store)) or 0
    chunk_total = _safe(lambda: chunk_eligible_total(store)) or 0

    for axis in load_axes():
        axis_id = str(axis["id"])
        namespace = axis_id.upper()
        level = axis.get("level") or "ref"
        if level == "chunk":
            result = _safe(
                lambda ns=namespace: chunk_axis_progress(store, ns, chunk_total)
            )
        else:
            result = _safe(lambda ns=namespace: ref_axis_progress(store, ns, pp_total))
        if result is None:
            rows[axis_id] = {"done": 0, "total": 0, "error": True, "last_minted": None}
        else:
            done, total, last_ts = result
            rows[axis_id] = {
                "done": done,
                "total": total,
                "error": False,
                "last_minted": last_ts,
            }

    topics = load_topics()
    if topics:
        enabled_slugs = enabled_topic_slugs(store)
        marker_value = current_marker_value(enabled_slugs)
        marker_done = (
            _safe(lambda mv=marker_value: topics_marker_done(store, mv))
            if marker_value
            else None
        )
        for topic in topics:
            slug = str(topic["slug"])
            hit_result = _safe(lambda s=slug: topic_hit_count(store, s))
            if marker_done is None or hit_result is None:
                rows[slug] = {
                    "done": 0,
                    "total": 0,
                    "hit_count": 0,
                    "error": True,
                    "last_minted": None,
                }
            else:
                hit_count, last_ts = hit_result
                rows[slug] = {
                    "done": marker_done,
                    "total": pp_total,
                    "hit_count": hit_count,
                    "error": False,
                    "last_minted": last_ts,
                }
    return rows


# ── per-ref machine-hint line (gr515633 ask 1) ──────────────────────────

#: The hint's missing-value wording, per axis. ``(not yet classified)`` =
#: the pass is on but hasn't reached this ref (or wrote nothing claimable);
#: ``no value`` = the pass ran this ref (a ``<NS>CASCADE`` marker is on it)
#: and wrote nothing — the ``view='facets'`` split, one line up.
NOT_YET = "(not yet classified)"
NO_VALUE = "(no value)"


def _ref_level_axis_ids(kind: str, *, opted_in: bool) -> list[str]:
    """Ref-level axis ids whose pass would claim a ref of ``kind`` —
    ``applies_to_kinds`` (default ``["paper"]``, mirroring
    ``axis_pass._claim_ref``) plus ``opt_in_kinds`` when the kind's own
    ``categorize_kind:<kind>`` service is on. Chunk-level axes are never in
    the line — their tags live on chunks, not the ref."""
    out: list[str] = []
    for axis in load_axes():
        if (axis.get("level") or "ref") != "ref":
            continue
        axis_id = str(axis["id"])
        applies = {str(k) for k in (axis.get("applies_to_kinds") or ["paper"])}
        if kind in applies or (
            opted_in and kind in {str(k) for k in (axis.get("opt_in_kinds") or [])}
        ):
            out.append(axis_id)
    return out


def ref_hint_line(
    store: Store, ref: Ref, *, tags: list[Tag] | None = None
) -> str | None:
    """The one-line categorizer hint for a ref's overview/eye render.

    Format: ``categorizers (machine — filter hint, not curated truth):
    DOMAIN=chemistry · MATERIAL=(not yet classified) · topic: mof``.
    The wording mirrors the taxon ``view='facets'`` caveat — these are
    pass outputs, not curated truth.

    Shown axes: every *enabled* ref-level axis whose claim set covers
    ``ref.kind`` (per :func:`effective_state` — so the line reflects what
    the passes actually run today, not the full yaml taxonomy), plus any
    axis the ref already carries data for even if its pass is now off.
    ``topic:`` lists the ref's ``topic:<slug>`` open tags when the
    ``classify_topics`` pass is on or the ref carries topic data;
    ``none`` (marker present, no topics) vs ``(not yet classified)``
    (pass on, ref untouched) reuse the facets distinction.

    ``None`` when there is nothing truthful to say — a kind no
    categorizer targets (cfp, memory, …), an opt-in kind
    (``workers/_claim.py`` :data:`OPT_IN_KINDS`) whose
    ``categorize_kind:<kind>`` service is off, or a corpus with every
    pass off and no categorizer data on the ref.

    ``tags=`` is the caller's already-fetched ``store.tags_for(ref.id)``
    when it has one in hand (the patent overview does) — saves the
    duplicate read.
    """
    from precis.workers._claim import OPT_IN_KINDS

    kind = ref.kind
    opted_in = False
    if kind in OPT_IN_KINDS:
        # The gripe's own gate: findings only get the line when the
        # operator opted the kind in (`categorize_kind:finding`).
        opted_in = opt_in_kind_enabled(store, kind)
        if not opted_in:
            return None

    axis_ids = _ref_level_axis_ids(kind, opted_in=opted_in)
    topics_apply = kind in ("paper", "patent") or (kind in OPT_IN_KINDS and opted_in)
    if not axis_ids and not topics_apply:
        return None

    if tags is None:
        tags = store.tags_for(ref.id)
    axis_values: dict[str, list[str]] = {}
    markers: set[str] = set()
    topic_values: list[str] = []
    for t in tags:
        if t.namespace == "closed" and t.prefix:
            if t.prefix.endswith("CASCADE"):
                markers.add(t.prefix)
            else:
                axis_values.setdefault(t.prefix, []).append(t.value)
        elif t.namespace == "open" and t.value.startswith("topic:"):
            topic_values.append(t.value[len("topic:") :])

    effective = _safe(lambda: effective_state(store)) or {}

    def _service_on(service: str) -> bool:
        return bool((effective.get(service) or {}).get("enabled"))

    parts: list[str] = []
    for axis_id in axis_ids:
        ns = axis_id.upper()
        on = _service_on(governing_service(axis_id))
        vals = axis_values.get(ns)
        marker = f"{ns}CASCADE" in markers
        if not on and not vals and not marker:
            continue
        if vals:
            shown = ",".join(sorted(set(vals)))
        elif marker:
            shown = NO_VALUE
        else:
            shown = NOT_YET
        parts.append(f"{ns}={shown}")

    if topics_apply:
        topics_on = _service_on("classify_topics")
        if topic_values:
            parts.append("topic: " + ", ".join(sorted(set(topic_values))))
        elif TOPIC_MARKER_NAMESPACE in markers:
            parts.append(f"topic: {NO_VALUE}")
        elif topics_on:
            parts.append(f"topic: {NOT_YET}")

    if not parts:
        return None
    # gr520232: when EVERY shown axis is pending, the per-axis dump is
    # noise on the landing view — collapse it to one honest clause. A
    # single-axis pending read stays expanded (it names the axis).
    if len(parts) > 1 and all(
        p.endswith(NOT_YET) or p.endswith(NO_VALUE) for p in parts
    ):
        return (
            "categorizers (machine — filter hint, not curated truth): "
            "all axes pending classification"
        )
    return "categorizers (machine — filter hint, not curated truth): " + " · ".join(
        parts
    )


# ── search coverage gate + exclusion note (gr515633 ask 2) ────────────
#
# ``tags=`` filters are AND/exclusive, so ``tags=['DOMAIN:chemistry']``
# silently drops every ref the pass hasn't reached yet. While coverage is
# below :data:`FILTER_READY_COVERAGE` the axes are deliberately NOT
# advertised as filters (search tool text / precis-search-help stay
# unchanged) and each filtered search appends the exclusion note instead.
# The advertisement seam is :func:`axis_filter_ready` — the same gate the
# note consults; once an axis crosses the bar its note goes quiet and the
# axis becomes eligible for advertising.

_COVERAGE_TTL_S = 300.0
_coverage_lock = threading.Lock()
#: ``key -> (fetched_at_monotonic, (done, total) | None)``
_coverage_cache: dict[str, tuple[float, tuple[int, int] | None]] = {}
#: ``key -> lock`` — the single-flight guard for cold inline computes:
#: N concurrent searches on one cold axis wait on the same lock instead
#: of each running the corpus scan (gr515633 review). Bounded by the
#: axis/topic count.
_key_locks: dict[str, threading.Lock] = {}
#: Coverage keys whose background fill is already running — the
#: single-flight guard for the chunk-axis path. Guarded by
#: ``_coverage_lock``.
_coverage_inflight: set[str] = set()


def _key_lock(key: str) -> threading.Lock:
    with _coverage_lock:
        lk = _key_locks.get(key)
        if lk is None:
            lk = _key_locks[key] = threading.Lock()
        return lk


def _cached_coverage(key: str, now: float) -> tuple[bool, tuple[int, int] | None]:
    """``(fresh, value)`` for ``key`` — ``value`` may itself be ``None``
    ("count unreadable" is a real cached verdict), so freshness travels
    in its own flag."""
    with _coverage_lock:
        hit = _coverage_cache.get(key)
        if hit is not None and now - hit[0] < _COVERAGE_TTL_S:
            return True, hit[1]
    return False, None


@functools.lru_cache(maxsize=4)
def _namespaces_for(
    _sig: tuple[tuple[str, int], ...],
) -> dict[str, tuple[str, str]]:
    return {
        str(a["id"]).upper(): (str(a["id"]), str(a.get("level") or "ref"))
        for a in load_axes()
    }


def _categorizer_namespaces() -> dict[str, tuple[str, str]]:
    """``{NAMESPACE: (axis_id, level)}`` for every axis yaml (any level) —
    the closed prefixes a ``tags=[...]`` filter can name that belong to the
    categorizer layer. Marker/bookkeeping namespaces (``<NS>CASCADE``,
    ``TOPICCASCADE``) are deliberately absent: they are pass bookkeeping,
    not a vocabulary a caller should filter on. Cached on the axes-dir
    signature (runs once per ``tags=`` search, not per tag)."""
    return _namespaces_for(_dir_signature(AXES_DIR))


def _coverage(store: Store, key: str, level: str) -> tuple[int, int] | None:
    """``(done, total)`` for one categorizer, reusing the progress-count
    queries — the same numbers ``progress_rows`` reports. ``key`` is an
    axis id, or ``"topic"`` for the ``classify_topics`` pass (done = refs
    carrying the live enabled-set marker). Process-cached for
    ``_COVERAGE_TTL_S``: a search's note must never cost a fresh corpus
    scan per call. ``None`` on any error — an unreadable count degrades
    to no note, never to a wrong "ready" verdict."""
    now = time.monotonic()
    hit, cov = _cached_coverage(key, now)
    if hit:
        return cov
    with _key_lock(key):
        hit, cov = _cached_coverage(key, time.monotonic())
        if hit:
            return cov
        result: tuple[int, int] | None = _safe(
            lambda: _coverage_uncached(store, key, level)
        )
        with _coverage_lock:
            _coverage_cache[key] = (now, result)
        return result


def _coverage_nonblocking(store: Store, key: str, level: str) -> tuple[int, int] | None:
    """``_coverage`` for hot-path callers that must not scan inline.

    Returns the fresh cached value when one exists; on a cold/stale key
    it kicks a single-flight daemon-thread fill and returns ``None``
    (no note now — the next search inside the TTL sees the filled
    cache). Used for chunk-level axes, whose ``total`` denominator is a
    whole-``chunks`` scan too expensive to run inside a user search."""
    now = time.monotonic()
    with _coverage_lock:
        hit = _coverage_cache.get(key)
        if hit is not None and now - hit[0] < _COVERAGE_TTL_S:
            return hit[1]
        if key in _coverage_inflight:
            return None
        _coverage_inflight.add(key)

    def _fill() -> None:
        try:
            result = _safe(lambda: _coverage_uncached(store, key, level))
            with _coverage_lock:
                _coverage_cache[key] = (time.monotonic(), result)
        finally:
            with _coverage_lock:
                _coverage_inflight.discard(key)

    threading.Thread(target=_fill, daemon=True, name=f"coverage-fill-{key}").start()
    return None


def _coverage_uncached(store: Store, key: str, level: str) -> tuple[int, int]:
    if key == "topic":
        marker_value = current_marker_value(enabled_topic_slugs(store))
        if not marker_value:
            return (0, paper_patent_total(store))
        return (topics_marker_done(store, marker_value), paper_patent_total(store))
    ns = key.upper()
    if level == "chunk":
        done, total, _ts = chunk_axis_progress(store, ns, chunk_eligible_total(store))
    else:
        done, total, _ts = ref_axis_progress(store, ns, paper_patent_total(store))
    return (done, total)


def axis_filter_ready(
    store: Store, axis_id: str, *, threshold: float = FILTER_READY_COVERAGE
) -> bool:
    """Whether ``axis_id`` covers ≥ ``threshold`` of its live population —
    the coverage gate the (future) search-filter advertisement hangs off.

    ``axis_id`` is an axis yaml id (``domain``, ``role3``…) or ``"topic"``
    for the ``classify_topics`` pass's marker coverage. ``False`` when the
    count is unreadable or the population is empty — an unknown coverage
    is never "ready".
    """
    level = (
        "ref"
        if axis_id == "topic"
        else _categorizer_namespaces().get(axis_id.upper(), (axis_id, "ref"))[1]
    )
    cov = _coverage(store, axis_id, level)
    if cov is None:
        return False
    done, total = cov
    return total > 0 and done / total >= threshold


def categorizer_filter_notes(store: Store, tags: Any) -> list[str]:
    """One warning line per categorizer axis named in a search's
    ``tags=`` that sits below the filter-ready bar:

        ``<AXIS>: N of M <population> not yet classified — excluded from
        this filter``

    where ``N`` is the still-unclassified share of the axis's live
    population (progress-count ``total - done``). Non-categorizer tags
    produce nothing; a ready axis produces nothing either — at ≥95%
    coverage the exclusive filter drops only the residue (and that axis
    is what the future advertisement will name)."""
    if isinstance(tags, str):
        tags = [tags]
    if not tags:
        return []
    namespaces = _categorizer_namespaces()
    notes: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        s = str(raw).strip()
        if not s:
            continue
        prefix, _, _value = s.partition(":")
        if prefix and prefix.isupper() and prefix in namespaces:
            key, level = namespaces[prefix]
            label = prefix
        elif s.lower().startswith("topic:"):
            key, level, label = "topic", "ref", "topic"
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        # Chunk-level axes never scan inside a search: their denominator
        # is a whole-chunks count. Cold cache → no note this call, and a
        # single-flight background fill warms it for the next one.
        cov = (
            _coverage_nonblocking(store, key, level)
            if level == "chunk"
            else _coverage(store, key, level)
        )
        if cov is None:
            continue
        done, total = cov
        if axis_filter_ready(store, key):
            continue
        population = "eligible chunks" if level == "chunk" else "paper+patent refs"
        notes.append(
            f"{label}: {total - done} of {total} {population} not yet "
            "classified — excluded from this filter"
        )
    return notes
