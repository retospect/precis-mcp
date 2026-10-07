"""Quest integration for the bulk Pourbaix job.

Operating conditions are a human-set quest input. Dispatch is content
addressed by geometry and those inputs; only a successful result for the
current input is harvested. The first slice records evidence but does not
rule out or rank a candidate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

log = logging.getLogger(__name__)


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
    values = {
        "pourbaix_verdict": point["verdict"],
        "pourbaix_worst_in_window": result.get("worst_in_window"),
        "pourbaix_dG_eV_atom": result.get("dG_pbx_eV_atom"),
        "pourbaix_domain": point.get("domain"),
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
    return text
