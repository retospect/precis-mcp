"""Seed the catalyst-discovery quest (first light) — NO→NH₃ on Pd(111).

A reproducible, **idempotent** minter for the flagship catalyst quest. It creates
a `quest` whose meta wires the whole loop:

* ``meta.reaction_config`` — autocatpath's worked NO→NH₃/Pd example
  (`examples/no_to_nh3_pd.yaml`). :func:`precis.quest.compute.run_compute_step`
  reads it and co-dispatches a autocatpath barrier eval with every candidate's relax.
* ``meta.rubric_objectives`` — the two measured axes that actually land **today**:
  the autocatpath ``barrier`` (min) and the relax ``energy`` (min, the stability
  proxy). ``formation_e`` is a future refinement — declaring an objective nothing
  produces would leave every candidate *unevaluated* (an empty frontier), so we
  rank on ``energy`` until formation energy is computed.
* ``meta.graduation`` — the in-silico ceiling that promotes a good design to a
  ``needs-experiment`` deed (:mod:`precis.quest.graduate`). A starting bar to tune.
* ``meta.param_space`` — non-chemistry scaffolding (coverage count, buildable
  facet) for the §7.8 optimizer advisor's ``(params → barrier, energy)``
  history. **Not a chemistry menu**: the discovery agent picks the dopant
  element, site, and co-adsorbate every tick using its own judgment (the
  explorer's creed + commit re-prompt in :mod:`precis.quest.tick`) — code
  never enumerates or constrains that choice.

**Dark by construction:** minting changes nothing until someone ticks it
(``precis quest tick <id> --compute``) or the autonomous loop is switched on.
Re-running returns the existing quest (matched by ``meta.seed_key``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

#: Stable marker on the quest's meta so the seed is idempotent (a quest has no
#: slug — this is how we find an already-minted one).
SEED_KEY = "no_to_nh3_pd"

STRIVING = (
    "Discover a palladium catalyst that minimises the rate-limiting barrier for "
    "NO→NH₃ (ammonia synthesis by NO reduction) on a Pd(111) surface, while "
    "keeping the slab stable. Each candidate is a `structure` (the model); autocatpath "
    "measures its reaction barrier and a relax measures its stability; the Pareto "
    "frontier ranks the barrier/stability trade-off."
)

#: autocatpath config the barrier lane runs (verbatim `no_to_nh3_pd.yaml`, backend
#: MACE per the design's first-light choice — an unrouted dev tick force-EMTs it).
REACTION_CONFIG: dict[str, Any] = {
    "name": "no_to_nh3_pd",
    "substrate": "NO",
    "target": "NH3",
    "network": "ammonia",
    "slab": {"element": "Pd", "size": [3, 3, 4], "vacuum": 10.0, "fix_layers": 2},
    "mlip": {"backend": "mace", "model": "medium", "device": "cuda"},
    "search": {
        "neb_images": 7,
        "fmax": 0.05,
        "max_steps": 200,
        "neb_fmax": 0.1,
        "neb_max_steps": 150,
        "seeds": [0, 1, 2],
        "rmsd_thresh": 0.7,
        "energy_thresh": 0.05,
    },
}

#: Rank on the two axes measured today: autocatpath barrier + relax energy (both min).
RUBRIC_OBJECTIVES: list[dict[str, str]] = [
    {"key": "barrier", "sense": "min"},
    {"key": "energy", "sense": "min"},
]

#: In-silico ceiling — a candidate whose rate-limiting barrier drops below this
#: (eV) graduates a candidate to a real-world experiment (a milestone, NOT a stop:
#: the search keeps hunting for a lower barrier — the objective is a moving best,
#: see the explorer's creed in tick.py). A tunable bar.
GRADUATION: dict[str, Any] = {"key": "barrier", "sense": "min", "threshold": 0.5}

#: Pure non-chemistry scaffolding for the §7.8 optimizer advisor's future
#: ``(params → barrier, energy)`` correlation study — NOT a chemistry menu.
#: `n_adatoms` is a coverage *count* (not which element); `facet` records a
#: buildability fact, not a design choice — only 111 is buildable today (the
#: `slab` op builds fcc111 via `ase.build.fcc111`; do NOT advertise 100/211
#: until a slab op supports them, or the model proposes unbuildable
#: candidates). The dopant element, its site (adatom / surface substitution /
#: subsurface), and any co-adsorbate are the discovery agent's own chemistry
#: judgment every tick — no code enumerates or constrains them (see the
#: explorer's creed + commit re-prompt in :mod:`precis.quest.tick`).
PARAM_SPACE: dict[str, Any] = {
    "n_adatoms": {"type": "int", "low": 0, "high": 4},
    "facet": {"type": "cat", "choices": ["111"]},
}


def _existing_seed(store: Store, seed_key: str) -> int | None:
    """The ref id of an already-seeded quest carrying ``meta.seed_key``, or None."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'quest' AND deleted_at IS NULL "
            "AND meta->>'seed_key' = %s ORDER BY ref_id ASC LIMIT 1",
            (seed_key,),
        ).fetchone()
    return int(row[0]) if row else None


def seed_catalyst_quest(
    store: Store,
    *,
    hub: Any | None = None,
    rubric_composite: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Mint (or return) the NO→NH₃/Pd catalyst quest.

    Returns ``(quest_ref_id, created)`` — ``created=False`` when an existing
    seeded quest was reused. Idempotent by ``meta.seed_key``.

    ``rubric_composite`` (default ``None`` = feature off) is the optional
    weighted-sum objective the caller has already decided on
    (``{"key": "score", "weights": {"barrier": 1.0, "U_L_abs": 0.5, ...}}``,
    see :mod:`precis.quest.frontier`) — the human-set electrochemistry rubric
    from docs/proposals/pathway-potential-lever.md. Written verbatim onto
    ``meta.rubric_composite`` at seed time only; nothing in the quest tick or
    the LLM loop may write this key later (the agent may not tune its own
    objective).
    """
    existing = _existing_seed(store, SEED_KEY)
    if existing is not None:
        return existing, False

    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    hub = hub or Hub(store=store)
    resp = QuestHandler(hub=hub).put(text=STRIVING)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    if m is None:  # pragma: no cover - put always echoes the handle
        raise RuntimeError(f"could not parse quest id from: {resp.body!r}")
    qid = int(m.group(1))
    meta: dict[str, Any] = {
        "seed_key": SEED_KEY,
        "reaction_config": REACTION_CONFIG,
        "rubric_objectives": RUBRIC_OBJECTIVES,
        "graduation": GRADUATION,
        "param_space": PARAM_SPACE,
    }
    if rubric_composite is not None:
        meta["rubric_composite"] = rubric_composite
    store.stamp_ref_meta(qid, meta)
    return qid, True


__all__ = ["REACTION_CONFIG", "SEED_KEY", "seed_catalyst_quest"]
