"""``pourbaix_bulk`` job_type — a recomputable bulk Pourbaix verdict for a
quest candidate's host phase over a U/pH window.

The job half of the bulk Pourbaix gate; the engine, verdict vocabulary and
pymatgen workaround are documented in :mod:`precis_dft.pourbaix_bulk`. The
quest gate (``docs/backlog/pourbaix-quest-gate.md``: dispatch, harvest,
rule-out) reads the verdict from this job's ``meta.verdict``, the same
pattern as the autocatpath harvest. Its first quest slice dispatches only
when human-set operating conditions exist and stamps successful results as
diagnostic evidence.
Deterministic in-process work registered with a plugin ``dispatch`` so
``claude_inproc`` runs it directly (no claude subprocess), like
``news_poll``.

One job:

1. resolves the candidate structure and splits its atoms into host,
   dopants and adsorbates (:func:`host_composition` — from the spec ops
   when the design was built from a ``slab`` op, else from the scene's
   z-layers; the path that ran and its rule are recorded in the result);
2. fetches Materials Project Pourbaix entries once per chemsys (host phase,
   plus each dopant's element), reusing the newest succeeded job's cached
   copy for the same (chemsys, MP version) key;
3. runs :func:`precis_dft.pourbaix_bulk.verdict` and writes the result to
   ``meta.verdict`` plus a ``job_summary`` chunk.

**Cache in job meta.** The entries are cached compactly (composition or
ion, energy, id, concentration — what a ``PourbaixDiagram`` reads) under
``meta.pourbaix_cache["<chemsys>@<mp_version>"]`` of the job that fetched
them; only fresh fetches are written. Not a chunk: a new chunk kind needs a
migration. Not the ``material`` kind: that is a CRC property store.

**Capability.** ``REQUIRES = {"has_pourbaix"}`` is satisfied statically by
``EXECUTOR_PROVIDES['claude_inproc']``, like ``ssh_node``'s ``has_gpaw``:
claude_inproc's claim does not check a job type's requirements per host.
What makes it true is the deploy: the ``[pourbaix]`` extra (pymatgen +
mp-api) is installed by BOTH the ``mcps`` role (``/opt/mcps/venv``, which
runs ``com.precis.worker-agent`` — the agent-profile worker that owns the
``claude_inproc`` pass and so claims these jobs) and the ``precis_worker``
role (the system worker venv). A host without it fails the job
``failure_class="config"`` at dispatch.

A missing ``PRECIS_MP_API_KEY`` (vault via ``get_secret``, process env
first, ADR-0055) fails the job ``failure_class="config"`` with no verdict —
never a silent "stable". A host chemsys over three non-O/H elements, a
malformed ``point``/``window`` or an unknown candidate fails
``failure_class="input"``; an MP fetch error ``failure_class="infra"``.
Ion-reference records use the supported MPContribs REST endpoint through
httpx, then feed the normal mp-api Pourbaix-entry workflow. This avoids the
optional contribs-client dependency conflict with the ``[estimate]`` Pint
bounds. The job preflights httpx and classifies a missing client as
``failure_class="config"``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.util import find_spec
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

#: Vault / env name of the Materials Project API key (ADR-0055).
MP_API_KEY_SECRET = "PRECIS_MP_API_KEY"

#: An element below this host atom fraction that entered as a substitution or
#: adatom (ops path) or sits only above the top layer (scene path) is a dopant.
DOPANT_FRACTION_MAX = 0.10

#: Elements an above-top-layer atom is read as an adsorbate for (scene
#: path), and a non-metal ``add_atom`` is read as an adsorbate for (ops path).
ADSORBATE_ELEMENTS = frozenset({"H", "C", "N", "O"})

#: ``meta`` key the fetched entries are cached under, keyed
#: ``"<chemsys>@<mp_version>"``.
CACHE_META_KEY = "pourbaix_cache"

_COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})

#: Satisfied by ``EXECUTOR_PROVIDES['claude_inproc']`` ⊇ ``{'has_pourbaix'}``,
#: a static declaration like ``ssh_node``'s ``has_gpaw``: the ``[pourbaix]``
#: extra is installed into every venv that runs the ``claude_inproc`` pass.
#: A host without it fails the job ``failure_class="config"`` at dispatch.
_REQUIRES = frozenset({"has_pourbaix"})

_RANGE = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
}

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["candidate_ref", "point"],
    "properties": {
        "candidate_ref": {
            "type": ["integer", "string"],
            "description": "The candidate structure: ref id, handle (st…) or slug.",
        },
        "point": {
            "type": "object",
            "required": ["U_RHE", "pH"],
            "properties": {
                "U_RHE": {"type": "number", "description": "V vs RHE."},
                "pH": {"type": "number"},
            },
            "additionalProperties": False,
        },
        "window": {
            "type": "object",
            "properties": {"U_RHE": _RANGE, "pH": _RANGE},
            "additionalProperties": False,
            "description": "Optional [lo, hi] per axis; an absent axis stays at the point.",
        },
        "ion_conc_M": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 10,
            "description": "Dissolved-ion activity (M); default 1e-6.",
        },
        "stability_tol": {
            "type": "number",
            "minimum": 0,
            "description": "eV/atom above the Pourbaix hull still called stable; default 0.1.",
        },
        "grid": {
            "type": "integer",
            "minimum": 2,
            "maximum": 21,
            "description": "Grid points per window axis; default 5.",
        },
    },
    "additionalProperties": False,
}


# ── host composition ──────────────────────────────────────────────────


@dataclass
class HostComposition:
    """How a candidate's atoms split into host, dopants and adsorbates."""

    #: ``"ops"`` (built from the spec ops) or ``"scene"`` (z-layer fallback).
    path: str
    #: Host atoms by element, dopants included.
    host: dict[str, int]
    #: Dopant element → host atom fraction.
    dopants: dict[str, float]
    #: Host minus dopants — what is matched to an MP phase.
    host_phase: dict[str, int]
    #: Adsorbate atoms by element.
    adsorbates: dict[str, int] = field(default_factory=dict)
    #: The rule that ran, in words (recorded in the result).
    rule: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rule": self.rule,
            "host": dict(sorted(self.host.items())),
            "dopants": {el: round(f, 6) for el, f in sorted(self.dopants.items())},
            "host_phase": dict(sorted(self.host_phase.items())),
            "adsorbates": dict(sorted(self.adsorbates.items())),
        }


_OPS_RULE = (
    "ops: host = the slab op's element + set_element targets + non-H/C/N/O "
    "add_atom elements; adsorbates = add_adsorbate groups + H/C/N/O add_atoms; "
    "a host element under 10% of host atoms that entered via set_element or "
    "add_atom is a dopant"
)
_SCENE_RULE = (
    "scene: every atom in the slab's layer stack is host; above-top-layer H, "
    "C, N, O atoms are adsorbates, other above-top-layer elements are host; "
    "an element under 10% of host atoms that sits only above the top layer "
    "is a dopant"
)

_FORMULA_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")


def _species_counts(species: str) -> dict[str, int]:
    """Element counts of an adsorbate group name (``"OH"``, ``"NH2"``)."""
    out: dict[str, int] = {}
    for el, n in _FORMULA_TOKEN.findall(species):
        out[el] = out.get(el, 0) + (int(n) if n else 1)
    return out


def _count(elements: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for el in elements:
        out[el] = out.get(el, 0) + 1
    return out


def _split_dopants(
    host: dict[str, int], candidates: set[str]
) -> tuple[dict[str, float], dict[str, int]]:
    total = sum(host.values())
    dopants = {
        el: host[el] / total
        for el in sorted(candidates)
        if total and el in host and host[el] / total < DOPANT_FRACTION_MAX
    }
    return dopants, {el: n for el, n in host.items() if el not in dopants}


def _ops_from_last_slab(ops: list[Any] | None) -> list[dict[str, Any]] | None:
    """The ops from the last ``slab`` op on (a ``slab`` clears the scene), or
    ``None`` when there is no ``slab`` op — the scene path then runs."""
    if not ops:
        return None
    idx = None
    for i, op in enumerate(ops):
        if isinstance(op, dict) and op.get("op") == "slab" and op.get("element"):
            idx = i
    return [op for op in ops[idx:] if isinstance(op, dict)] if idx is not None else None


def host_composition(scene: Any, ops: list[Any] | None = None) -> HostComposition:
    """Split a candidate's atoms into host, dopants and adsorbates.

    Ops path (``ops`` contain a ``slab`` op): the element *roles* come from
    the ops (the substitution op is ``set_element``), the *counts* from the
    materialised ``scene``. Scene path (no ``slab`` op — an imported or
    hand-placed design): roles come from the z-layers
    (:func:`precis.structure.invariants._layers`), the sparse clusters above
    the top dense layer being "above the top layer".
    """
    scene_counts = _count(a.element for a in scene.atoms.values())
    built = _ops_from_last_slab(ops)
    if built is not None:
        slab_el = str(built[0]["element"])
        host_elts = {slab_el}
        entered: set[str] = set()
        ads: dict[str, int] = {}
        for op in built:
            name, el = op.get("op"), op.get("element")
            if name == "set_element" and isinstance(el, str) and el:
                host_elts.add(el)
                entered.add(el)
            elif name in ("add_atom", "add_atom_site") and isinstance(el, str) and el:
                if el in ADSORBATE_ELEMENTS:
                    ads[el] = ads.get(el, 0) + 1
                else:
                    host_elts.add(el)
                    entered.add(el)
            elif name == "add_adsorbate" and isinstance(op.get("species"), str):
                for sel, n in _species_counts(str(op["species"])).items():
                    ads[sel] = ads.get(sel, 0) + n
        # An adsorbate atom of a host element (an O adsorbate on a host whose
        # set_element target is O) is not a host atom.
        host = {
            el: n - ads.get(el, 0)
            for el, n in scene_counts.items()
            if el in host_elts and n - ads.get(el, 0) > 0
        }
        dopants, host_phase = _split_dopants(host, entered - {slab_el})
        return HostComposition(
            path="ops",
            host=host,
            dopants=dopants,
            host_phase=host_phase,
            adsorbates={el: n for el, n in ads.items() if n},
            rule=_OPS_RULE,
        )

    from precis.structure.invariants import _layers, _split_slab_adsorbate

    layers = _layers(scene)
    _surface, above = _split_slab_adsorbate(scene, layers)
    above_set = set(above)
    host_labels = [
        la
        for la, a in scene.atoms.items()
        if la not in above_set or a.element not in ADSORBATE_ELEMENTS
    ]
    host = _count(scene.atoms[la].element for la in host_labels)
    ads = _count(
        a.element
        for la, a in scene.atoms.items()
        if la in above_set and a.element in ADSORBATE_ELEMENTS
    )
    in_stack = {scene.atoms[la].element for la in host_labels if la not in above_set}
    only_above = set(host) - in_stack
    dopants, host_phase = _split_dopants(host, only_above)
    return HostComposition(
        path="scene",
        host=host,
        dopants=dopants,
        host_phase=host_phase,
        adsorbates=ads,
        rule=_SCENE_RULE,
    )


# ── candidate + cache lookups ─────────────────────────────────────────


def _resolve_candidate(store: Any, candidate_ref: int | str) -> Any | None:
    """The live ``structure`` ref for an id, a handle (``st123``) or a slug."""
    if isinstance(candidate_ref, int) or str(candidate_ref).isdigit():
        return store.get_ref(kind="structure", id=int(candidate_ref))
    from precis.utils import handle_registry

    parsed = handle_registry.parse(str(candidate_ref))
    if parsed is not None and parsed[0] == "structure" and not parsed[1]:
        return store.get_ref(kind="structure", id=int(parsed[2]))
    return store.get_ref(kind="structure", id=str(candidate_ref))


def _candidate_ops(store: Any, ref_id: int) -> list[Any] | None:
    """Every recorded revision's ops, oldest first, or ``None`` when the
    design has no revision record."""
    from precis.design.history import list_revisions

    revs = list_revisions(store, ref_id)
    if not revs:
        return None
    return [op for rev in revs for op in rev.ops]


def cache_key(chemsys: str, mp_version: str) -> str:
    return f"{chemsys}@{mp_version}"


def find_cached_entries(store: Any, key: str) -> tuple[int, list[Any]] | None:
    """``(job_id, entries_json)`` from the newest succeeded ``pourbaix_bulk``
    job whose ``meta.pourbaix_cache`` holds ``key``, or ``None``.

    A ``_find_job_by_idem_key``-style lookup: newest ``ref_id`` first, any
    job that did not reach ``STATUS:succeeded`` is skipped.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->%(meta)s->%(key)s FROM refs "
            "WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'job_type' = 'pourbaix_bulk' "
            "AND meta->%(meta)s ? %(key)s "
            "ORDER BY ref_id DESC LIMIT 20",
            {"meta": CACHE_META_KEY, "key": key},
        ).fetchall()
    for ref_id, entries in rows:
        if not isinstance(entries, list):
            continue
        if any(str(t) == "STATUS:succeeded" for t in store.tags_for(int(ref_id))):
            return int(ref_id), entries
    return None


# ── MP seams (tests replace these; no network in the suite) ───────────


def _mp_version(api_key: str) -> str:
    from precis_dft.pourbaix_bulk import database_version

    return database_version(api_key)


def _mp_fetch(api_key: str, chemsys: str) -> list[Any]:
    from precis_dft.pourbaix_bulk import fetch_entries

    return fetch_entries(api_key, chemsys)


MP_VERSION: Callable[[str], str] = _mp_version
MP_FETCH: Callable[[str, str], list[Any]] = _mp_fetch


def _api_key() -> str:
    from precis import secrets as _secrets

    return (_secrets.get_secret(MP_API_KEY_SECRET) or "").strip()


# ── params ────────────────────────────────────────────────────────────


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_params(params: dict[str, Any]) -> str | None:
    """The nested checks ``JobHandler``'s shallow schema validator skips
    (point/window shape, positive concentration, grid range), or ``None``."""
    point = params.get("point")
    if not isinstance(point, dict) or not all(
        _is_number(point.get(k)) for k in ("U_RHE", "pH")
    ):
        return "params.point must be {'U_RHE': <V vs RHE>, 'pH': <number>}"
    window = params.get("window")
    if window is not None:
        if not isinstance(window, dict) or set(window) - {"U_RHE", "pH"}:
            return "params.window must be {'U_RHE': [lo, hi], 'pH': [lo, hi]}"
        for axis, span in window.items():
            if not (
                isinstance(span, list)
                and len(span) == 2
                and all(_is_number(x) for x in span)
            ):
                return f"params.window.{axis} must be [lo, hi]"
    conc = params.get("ion_conc_M")
    if conc is not None and not (_is_number(conc) and 0 < conc <= 10):
        return "params.ion_conc_M must be a number in (0, 10]"
    tol = params.get("stability_tol")
    if tol is not None and not (_is_number(tol) and tol >= 0):
        return "params.stability_tol must be a number >= 0"
    grid = params.get("grid")
    if grid is not None and not (
        isinstance(grid, int) and not isinstance(grid, bool) and 2 <= grid <= 21
    ):
        return "params.grid must be an integer in [2, 21]"
    return None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Submit-time rejection of malformed params (``JobTypeSpec.
    validate_submit``); ``store``/``gripe_id`` are the registry's uniform
    signature, unused here."""
    del store, gripe_id
    return check_params(params)


# ── dispatch ──────────────────────────────────────────────────────────


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    params = (ctx.meta or {}).get("params") or {}
    bad = check_params(params)
    if bad is not None:
        ctx.record_failure(f"pourbaix_bulk: {bad}", failure_class="input")
        return
    missing = [m for m in ("pymatgen", "mp_api", "httpx") if find_spec(m) is None]
    if missing:
        ctx.record_failure(
            f"pourbaix_bulk: {', '.join(missing)} not installed on this host "
            "(the [pourbaix] extra)",
            failure_class="config",
        )
        return
    api_key = _api_key()
    if not api_key:
        ctx.record_failure(
            f"pourbaix_bulk: no {MP_API_KEY_SECRET} (vault or process env) — "
            "no verdict without Materials Project entries",
            failure_class="config",
        )
        return

    from precis_dft import pourbaix_bulk as engine

    ref = _resolve_candidate(ctx.store, params.get("candidate_ref", ""))
    if ref is None:
        ctx.record_failure(
            f"pourbaix_bulk: no live structure {params.get('candidate_ref')!r}",
            failure_class="input",
        )
        return
    scene, _handles = ctx.store.structure_load(ref.id)
    comp = host_composition(scene, _candidate_ops(ctx.store, ref.id))
    host_elts = set(comp.host_phase) - {"O", "H"}
    if not host_elts:
        ctx.record_failure(
            f"pourbaix_bulk: structure {ref.id} has no non-O/H host phase "
            f"({comp.path} path)",
            failure_class="input",
        )
        return
    if len(host_elts) > engine.MAX_CHEMSYS_ELEMENTS:
        ctx.record_failure(
            f"pourbaix_bulk: host chemsys {engine.chemsys(host_elts)} has "
            f"{len(host_elts)} non-O/H elements; v1 handles at most "
            f"{engine.MAX_CHEMSYS_ELEMENTS}",
            failure_class="input",
        )
        return

    systems = [engine.chemsys(host_elts)] + [
        engine.chemsys({el}) for el in sorted(comp.dopants) if el not in ("O", "H")
    ]
    try:
        mp_version = MP_VERSION(api_key)
        rows: list[dict[str, Any]] = []
        fresh: dict[str, list[dict[str, Any]]] = {}
        sources: dict[str, str] = {}
        for system in dict.fromkeys(systems):
            key = cache_key(system, mp_version)
            hit = find_cached_entries(ctx.store, key)
            if hit is not None:
                sources[system] = f"cache:job {hit[0]}"
                rows.extend(hit[1])
                continue
            fetched = engine.entries_to_json(MP_FETCH(api_key, system))
            fresh[key] = fetched
            sources[system] = "mp"
            rows.extend(fetched)
    except Exception as exc:
        log.warning("pourbaix_bulk: MP fetch failed", exc_info=True)
        ctx.record_failure(
            f"pourbaix_bulk: Materials Project fetch failed: {exc}",
            failure_class="infra",
        )
        return

    point = params["point"]
    try:
        result = engine.verdict(
            engine.entries_from_json(rows),
            comp.host_phase,
            comp.dopants,
            point,
            params.get("window"),
            params.get("stability_tol", engine.DEFAULT_STABILITY_TOL),
            ion_conc_M=params.get("ion_conc_M", engine.DEFAULT_ION_CONC_M),
            grid=params.get("grid", engine.DEFAULT_GRID),
            mp_version=mp_version,
            composition_path=comp.path,
        )
    except ValueError as exc:
        ctx.record_failure(f"pourbaix_bulk: {exc}", failure_class="input")
        return
    result["candidate_ref_id"] = int(ref.id)
    result["composition"] = comp.as_dict()
    result["basis"]["entries_source"] = sources
    meta: dict[str, Any] = {"verdict": result}
    if fresh:
        meta[CACHE_META_KEY] = fresh
    ctx.set_meta(**meta)
    ctx.append_chunk("job_summary", result["text"])


SPEC = JobTypeSpec(
    name="pourbaix_bulk",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=_COMPATIBLE_EXECUTORS,
    requires=_REQUIRES,
    description=(
        "Bulk Pourbaix verdict (dissolved/leached/transformed/oxidised/"
        "unmatched/stable) for a candidate structure's host phase at a U/pH "
        "point and over a window, from Materials Project entries."
    ),
    validate_submit=validate_submit,
    dispatch=_dispatch,
)

PARAMS_SCHEMA = _PARAMS_SCHEMA
REQUIRES = _REQUIRES


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "PARAMS_SCHEMA",
    "REQUIRES",
    "SPEC",
    "HostComposition",
    "cache_key",
    "check_params",
    "find_cached_entries",
    "host_composition",
    "load",
    "validate_submit",
]
