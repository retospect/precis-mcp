"""Quest integration for the bulk Pourbaix job.

Operating conditions are a human-set quest input. Dispatch is content
addressed by geometry and those inputs; only a successful result for the
current input is harvested. A host-phase verdict dissolved across the
whole operating window tags the candidate
:data:`RULED_OUT_TAG`; a later verdict under changed operating inputs
that no longer rules it out lifts the tag (one logbook entry each way).
Verdicts never rank a candidate — every ``pourbaix_*`` stamp is in
``frontier._META_NON_MEASURE``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

log = logging.getLogger(__name__)

#: A candidate whose host phase dissolves across the whole operating
#: window stops consuming selectivity budget
#: (``docs/backlog/pourbaix-quest-gate.md`` §Rule-out / lift slice). The
#: prefix is what ``harvest_measures``' ``already_out`` guard and every
#: ``ruled-out:`` surface (tick, status, explore, frontier lineage) match.
RULED_OUT_TAG = "ruled-out:pourbaix-dissolved"


def _request(
    meta: dict[str, Any] | None, candidate: Any
) -> tuple[dict[str, Any], str] | None:
    """Build validated job params and idem key; missing geometry fails closed."""
    conditions = (meta or {}).get("operating_conditions")
    geometry = (candidate.meta or {}).get("geom_hash_c")
    if (
        not isinstance(conditions, dict)
        or not isinstance(conditions.get("U_RHE"), (int, float))
        or isinstance(conditions.get("U_RHE"), bool)
        or not math.isfinite(float(conditions.get("U_RHE", 0)))
        or not isinstance(conditions.get("pH"), (int, float))
        or isinstance(conditions.get("pH"), bool)
        or not math.isfinite(float(conditions.get("pH", 0)))
        or not isinstance(geometry, str)
        or not geometry
    ):
        return None
    params: dict[str, Any] = {
        "candidate_ref": candidate.id,
        "point": {"U_RHE": conditions["U_RHE"], "pH": conditions["pH"]},
        "ion_conc_M": conditions.get("ion_conc_M", 1e-6),
    }
    if "window" in conditions:
        params["window"] = conditions["window"]
    basis = {"geom_hash_c": geometry, **params}
    token = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return params, f"pourbaix_bulk:{candidate.id}:{token}"


def dispatch_pourbaix(store: Any, quest_id: int, *, hub: Any) -> list[str]:
    """Dispatch once per live candidate and geometry/operating-input pair."""
    from precis.quest.compute import _find_job_by_idem_key
    from precis.quest.gaps import _live_servers

    quest = store.fetch_refs_by_ids({quest_id}).get(quest_id)
    if quest is None or not isinstance(
        (quest.meta or {}).get("operating_conditions"), dict
    ):
        return []
    job_handler = hub.handler_for("job")
    if job_handler is None:
        return ["pourbaix_bulk dispatch skipped: job handler unavailable"]

    notes: list[str] = []
    for candidate in _live_servers(store, quest_id):
        if candidate.kind != "structure":
            continue
        built = _request(quest.meta, candidate)
        if built is None:
            notes.append(
                f"pourbaix_bulk skipped: structure {candidate.id} has no geometry hash"
            )
            continue
        params, idem_key = built
        if _find_job_by_idem_key(store, idem_key) is not None:
            continue
        try:
            response = job_handler.put(
                parent_id=candidate.id,
                job_type="pourbaix_bulk",
                executor="claude_inproc",
                params=params,
                idem_key=idem_key,
            )
        except Exception as exc:
            log.warning(
                "pourbaix_bulk dispatch failed for structure %s",
                candidate.id,
                exc_info=True,
            )
            notes.append(
                f"pourbaix_bulk dispatch failed for structure {candidate.id}: {exc}"
            )
        else:
            notes.append(response.body)
    return notes


def harvest_pourbaix_candidate(store: Any, quest_id: int, candidate: Any) -> str | None:
    """Stamp a succeeded current-input verdict once; failures are not verdicts."""
    from precis.quest.compute import _find_job_by_idem_key
    from precis.quest.logbook import MEASURED_BY, append_entry

    quest = store.fetch_refs_by_ids({quest_id}).get(quest_id)
    if quest is None:
        return None
    built = _request(quest.meta, candidate)
    if built is None:
        return None
    params, idem_key = built
    found = _find_job_by_idem_key(store, idem_key)
    if found is None:
        return None
    job_id, status = found
    if status != "succeeded" or (candidate.meta or {}).get("pourbaix_job_id") == job_id:
        return None
    job = store.fetch_refs_by_ids({job_id}).get(job_id)
    result = (job.meta or {}).get("verdict") if job is not None else None
    if not isinstance(result, dict) or not isinstance(result.get("basis"), dict):
        return None
    point = result.get("point")
    if not isinstance(point, dict) or not isinstance(point.get("verdict"), str):
        return None
    basis = dict(result["basis"])
    if (
        basis.get("U_RHE") != params["point"]["U_RHE"]
        or basis.get("pH") != params["point"]["pH"]
        or basis.get("ion_conc_M") != params["ion_conc_M"]
        or basis.get("window") != params.get("window")
    ):
        return None
    basis["geom_hash_c"] = (candidate.meta or {}).get("geom_hash_c")
    basis["operating_conditions"] = (quest.meta or {}).get("operating_conditions")
    old_basis = (candidate.meta or {}).get("pourbaix_basis")
    values = {
        "pourbaix_verdict": point["verdict"],
        "pourbaix_worst_in_window": result.get("worst_in_window"),
        "pourbaix_dG_eV_atom": result.get("dG_pbx_eV_atom"),
        "pourbaix_domain": point.get("domain"),
        "pourbaix_dissolved_everywhere": result.get("dissolved_everywhere"),
        "pourbaix_verdict_source": result.get("verdict_source"),
        "pourbaix_basis": basis,
        "pourbaix_job_id": job_id,
    }
    store.stamp_ref_meta(candidate.id, values)
    from precis.utils import handle_registry

    handle = (
        handle_registry.try_format("structure", candidate.id)
        or f"structure:{candidate.id}"
    )
    text = (
        f"bulk Pourbaix result for [{handle}]: {point['verdict']} at "
        f"U_RHE={point.get('U_RHE')} V, pH={point.get('pH')}"
    )
    append_entry(store, quest_id, text=text, entry_type="result", by=MEASURED_BY)
    gate = _gate_candidate(
        store,
        quest_id,
        candidate,
        conditions=(quest.meta or {}).get("operating_conditions") or {},
        result=result,
        point=point,
        basis=basis,
        old_basis=old_basis,
        handle=handle,
    )
    return f"{text}; {gate}" if gate else text


def _rules_out(
    conditions: dict[str, Any], result: dict[str, Any], point: dict[str, Any]
) -> bool:
    """Host phase dissolved across the whole window — the rule-out predicate.

    Only a windowed quest rules out (a point-only verdict never does) and
    only the host-level result counts: a dopant's ``elemental``
    sub-verdict never reaches this predicate — it lives in
    ``result["dopants"]``, not on the top-level keys read here."""
    return (
        bool(conditions.get("window"))
        and result.get("dissolved_everywhere") is True
        and result.get("verdict_source") == "host_phase"
        and point.get("verdict") != "unmatched"
    )


def _basis_brief(basis: Any) -> str:
    """The operating inputs of a ``pourbaix_basis`` dict, for logbook text."""
    if not isinstance(basis, dict):
        return "no recorded basis"
    return (
        f"U_RHE={basis.get('U_RHE')} V, pH={basis.get('pH')}, "
        f"ion_conc_M={basis.get('ion_conc_M')}, window={basis.get('window')}"
    )


def _gate_candidate(
    store: Any,
    quest_id: int,
    candidate: Any,
    *,
    conditions: dict[str, Any],
    result: dict[str, Any],
    point: dict[str, Any],
    basis: dict[str, Any],
    old_basis: Any,
    handle: str,
) -> str | None:
    """Set or lift :data:`RULED_OUT_TAG` for the just-stamped verdict.

    Never consults ``harvest_measures``' ``already_out`` guard — a
    candidate carrying another ``ruled-out:*`` tag can still earn this
    one, and this tag in turn suppresses a later relax-failed ruling
    (accepted: the candidate is already out). The lift fires only here,
    and harvest returns early unless a succeeded job for the *current*
    operating inputs was just found — so cleared ``operating_conditions``
    or a failed/queued new-input job leaves an old tag in place until
    the next succeeded harvest (accepted).
    """
    from precis.quest.logbook import MEASURED_BY, append_entry
    from precis.store import Tag

    title = getattr(candidate, "title", None)
    name = title.splitlines()[0] if title else handle
    has_tag = RULED_OUT_TAG in {str(t) for t in store.tags_for(candidate.id)}
    if _rules_out(conditions, result, point):
        if has_tag:
            return None
        store.add_tag(candidate.id, Tag.open(RULED_OUT_TAG), set_by="system")
        append_entry(
            store,
            quest_id,
            text=(
                f"ruled out [{handle}] ({name}): host phase dissolved across "
                f"the operating window ({_basis_brief(basis)})"
            ),
            entry_type="dead-end",
            by=MEASURED_BY,
        )
        return f"ruled-out [{handle}]"
    if not has_tag:
        return None
    store.remove_tag(candidate.id, Tag.open(RULED_OUT_TAG))
    append_entry(
        store,
        quest_id,
        text=(
            f"Pourbaix rule-out lifted for [{handle}] ({name}): no longer "
            f"dissolved across the operating window "
            f"(was: {_basis_brief(old_basis)}; now: {_basis_brief(basis)})"
        ),
        entry_type="note",
        by=MEASURED_BY,
    )
    return f"lifted pourbaix rule-out for [{handle}]"
