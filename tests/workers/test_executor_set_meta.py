"""``executors._common.set_meta`` must survive non-finite floats.

Regression pin for the batch-2 seed shredding
(docs/backlog/autocatpath-seed-child-killed-shredding.md): Python's json
round-trips ``Infinity``/``NaN`` but Postgres ``jsonb`` rejects the tokens,
so an unsanitized ``Jsonb(fields)`` write raised
``InvalidTextRepresentation`` — discarding 48 finished GPU seed results and
misclassifying them as ``infra:child-killed``. Real PG on purpose: a mocked
store cannot catch this class of bug (see ``psycopg_percent_like_
fakestore_gap``).
"""

from __future__ import annotations

import math

from precis.store.store import Store
from precis.workers.executors import _common


class TestFiniteJson:
    def test_maps_non_finite_floats_to_none_recursively(self) -> None:
        out = _common._finite_json(
            {
                "a": float("inf"),
                "b": [1.5, float("-inf"), {"c": float("nan")}],
                "d": (2, float("inf")),
            }
        )
        assert out == {"a": None, "b": [1.5, None, {"c": None}], "d": [2, None]}

    def test_finite_values_pass_through_untouched(self) -> None:
        payload = {"x": 1.25, "y": "inf", "z": [0, -3, True, None]}
        assert _common._finite_json(payload) == payload
        assert math.isfinite(_common._finite_json(1e308))


def test_set_meta_persists_a_payload_carrying_inf_and_nan(store: Store) -> None:
    # The exact shape that killed batch 2: trust evidence with
    # min_dist_A = inf (a single-atom adsorbate has no pair distance).
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('job', 'seed') RETURNING ref_id"
        ).fetchone()
        assert row is not None
        ref_id = int(row[0])
        _common.set_meta(
            conn,
            ref_id,
            partial={
                "states": {
                    "N": {"evidence": {"min_dist_A": float("inf"), "state": "N"}}
                },
                "score": float("nan"),
            },
        )
        got = conn.execute(
            "SELECT meta->'partial' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert got is not None
        assert got[0] == {
            "states": {"N": {"evidence": {"min_dist_A": None, "state": "N"}}},
            "score": None,
        }
