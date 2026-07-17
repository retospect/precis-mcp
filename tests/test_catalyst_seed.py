"""Tests for the catalyst-quest seed (`precis.quest.catalyst_seed`).

The seed mints the NO→NH₃/Pd quest with the meta that wires first light:
`reaction_config` (catpath's Pd example → co-dispatch), `rubric_objectives`
(barrier + energy), `graduation` ceiling, `param_space`. Idempotent by
`meta.seed_key`. Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

from typing import Any

from precis.quest.catalyst_seed import (
    REACTION_CONFIG,
    SEED_KEY,
    seed_catalyst_quest,
)
from precis.quest.compute import _quest_reaction_config
from precis.quest.frontier import _objectives_for
from precis.quest.graduate import graduation_rule


def _meta(store: Any, qid: int) -> dict[str, Any]:
    return store.fetch_refs_by_ids({qid})[qid].meta or {}


class TestSeedCatalystQuest:
    def test_mints_quest_with_first_light_meta(self, store: Any) -> None:
        qid, created = seed_catalyst_quest(store)
        assert created is True
        meta = _meta(store, qid)
        assert meta["seed_key"] == SEED_KEY
        # reaction config drives the catpath co-dispatch lane
        assert meta["reaction_config"] == REACTION_CONFIG
        assert _quest_reaction_config(store, qid) == REACTION_CONFIG
        # it is a live quest
        assert store.fetch_refs_by_ids({qid})[qid].kind == "quest"

    def test_objectives_and_graduation_are_wired(self, store: Any) -> None:
        qid, _ = seed_catalyst_quest(store)
        # the frontier ranks on the two axes that actually land today
        assert _objectives_for(store, qid) == [("barrier", "min"), ("energy", "min")]
        # the ceiling promotes a low-barrier design to needs-experiment
        rule = graduation_rule(store, qid)
        assert rule is not None
        key, sense, threshold = rule
        assert key == "barrier" and sense == "min" and threshold > 0

    def test_idempotent_reuse(self, store: Any) -> None:
        qid1, created1 = seed_catalyst_quest(store)
        qid2, created2 = seed_catalyst_quest(store)
        assert created1 is True and created2 is False
        assert qid1 == qid2  # same quest, matched by seed_key

    def test_reaction_config_is_a_valid_catpath_shape(self, store: Any) -> None:
        # the config carries what the catpath bridge needs to build + run
        assert REACTION_CONFIG["substrate"] == "NO"
        assert REACTION_CONFIG["target"] == "NH3"
        assert REACTION_CONFIG["network"] == "ammonia"
        assert REACTION_CONFIG["slab"]["element"] == "Pd"
