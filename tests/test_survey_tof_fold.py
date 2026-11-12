"""Migration 0309: fold the duplicate TOF measurand (gr638050).

0305's survey seed minted a `tof` node (name 'TOF', alias 'turnover
frequency') because its insert-only-missing guard dedupes on slug only;
the hand-put `turnover-frequency` node (alias 'TOF') has a different
slug, so one quantity split into two live claimants of the 'tof' surface
and every draft edit naming TOF warned "ambiguous label 'tof'". 0309
folds the seeded loser into the hand-put winner: it fires only when the
two share a name/alias surface (the same-quantity evidence — on prod
each node names the other), the winner keeps the folded surface (alias
'TOF' added when absent), the loser's edges move over (the garden
merge's migrate_links semantics), its measures re-point through the
0234 door when the unit contract matches, and the loser retires with a
`supersedes` trail.

Real PG, same replay harness as test_survey_measurand_seed: the store
fixture's per-test TRUNCATE leaves `refs` empty, so each test replays
0174 + 0305, plants the prod-shaped `turnover-frequency` winner by
hand, and runs 0309. On a fresh replay (no winner) the fold is a
no-op, so the fixture's own schema build is unaffected.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

from precis.reading.concepts import normalize_name
from precis.store import Store
from precis.taxonomy.nodes import slugify


def _run_migration(name: str) -> None:
    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            _execute_dump_sql(cur, sql)


_SEED = "0305_survey_measurand_seed.sql"
_FOLD = "0309_survey_tof_fold.sql"


@pytest.fixture
def seeded(store: Store) -> Store:
    """The measurand start node (0174) plus the survey seed."""
    _run_migration("0174_taxon_seed.sql")
    _run_migration(_SEED)
    return store


def _one(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    """``fetchone()`` asserted non-None — the row must exist."""
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row


def _seeded_tof_id(store: Store) -> int:
    with store.pool.connection() as conn:
        (rid,) = _one(
            conn,
            "SELECT ref_id FROM refs WHERE kind = 'taxon' "
            "AND retired_at IS NULL AND meta->>'slug' = 'tof'",
        )
    return int(rid)


def _put_winner(
    store: Store,
    *,
    aliases: list[str],
    canonical_unit: str = "1/s",
    si_vector: str = "0,0,-1,0,0,0,0",
) -> int:
    """The prod-shaped hand-put `turnover-frequency` node: a live taxon
    under `measurand` with its card and specialises edge, mirroring how
    tn520139 predates the seed."""
    with store.pool.connection() as conn:
        (root,) = _one(
            conn,
            "SELECT ref_id FROM refs WHERE kind = 'taxon' "
            "AND retired_at IS NULL AND meta->>'start' = 'true' "
            "AND meta->>'slug' = 'measurand' ORDER BY ref_id LIMIT 1",
        )
        (rid,) = _one(
            conn,
            "INSERT INTO refs (kind, title, meta) VALUES ('taxon', %s, %s) "
            "RETURNING ref_id",
            (
                "Turnover frequency",
                json.dumps(
                    {
                        "name": "Turnover frequency",
                        "norm_name": "turnover frequency",
                        "slug": "turnover-frequency",
                        "definition": (
                            "Turnover frequency is the number of product "
                            "molecules one active site produces per second."
                        ),
                        "aliases": aliases,
                        "status": "proposed",
                        "dimension_kind": "si",
                        "si_vector": si_vector,
                        "canonical_unit": canonical_unit,
                        "value_type": "quantity",
                    }
                ),
            ),
        )
        winner = int(rid)
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, -1, 'card_combined', %s, '{}'::jsonb)",
            (winner, "Turnover frequency — the hand-put winner"),
        )
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, 'specialises', 'system')",
            (winner, int(root)),
        )
    return winner


def _claimants(store: Store, surface: str) -> list[int]:
    """Live taxa claiming ``surface`` — the store-side of the draft
    lint's _term_nodes and the reader vocabulary's resolution leg."""
    return store.taxon_find_by_term(slug=slugify(surface), norm=normalize_name(surface))


def _loser_retired(store: Store, loser: int) -> bool:
    with store.pool.connection() as conn:
        (retired,) = _one(
            conn, "SELECT retired_at IS NOT NULL FROM refs WHERE ref_id = %s", (loser,)
        )
    return bool(retired)


def _winner_meta(store: Store, winner: int) -> dict[str, Any]:
    ref = store.get_ref(kind="taxon", id=winner)
    assert ref is not None
    return dict(ref.meta or {})


def test_fold_retires_the_seed_mint_and_keeps_one_tof_claimant(
    seeded: Store,
) -> None:
    store = seeded
    loser = _seeded_tof_id(store)
    winner = _put_winner(store, aliases=["TOF"])
    assert sorted(_claimants(store, "tof")) == sorted([loser, winner])

    _run_migration(_FOLD)

    assert _claimants(store, "tof") == [winner]
    assert _loser_retired(store, loser)
    meta = _winner_meta(store, winner)
    assert meta["slug"] == "turnover-frequency"  # the winner keeps its identity
    assert "TOF" in meta["aliases"]
    # The fold is recorded and the retirement leaves the 0229 trail.
    with store.pool.connection() as conn:
        (edge,) = _one(
            conn,
            "SELECT count(*) FROM links WHERE src_ref_id = %s "
            "AND dst_ref_id = %s AND relation = 'supersedes'",
            (winner, loser),
        )
        (event,) = _one(
            conn,
            "SELECT count(*) FROM ref_events WHERE ref_id = %s AND event = 'retired'",
            (loser,),
        )
    assert edge == 1 and event == 1


def test_fold_is_idempotent(seeded: Store) -> None:
    store = seeded
    _put_winner(store, aliases=["TOF"])
    _run_migration(_FOLD)
    after_first = _claimants(store, "tof")
    _run_migration(_FOLD)
    assert _claimants(store, "tof") == after_first


def test_fold_adds_the_tof_alias_when_the_winner_lacks_it(seeded: Store) -> None:
    """The shared-surface evidence here is the loser's own alias
    'turnover frequency' — the winner's name — so the fold fires even
    though the winner does not claim 'tof' yet, and the fold keeps the
    surface by adding the alias."""
    store = seeded
    _seeded_tof_id(store)
    winner = _put_winner(store, aliases=["catalyst turnover"])
    _run_migration(_FOLD)
    assert _claimants(store, "tof") == [winner]
    meta = _winner_meta(store, winner)
    assert "TOF" in meta["aliases"] and "catalyst turnover" in meta["aliases"]
    # The card re-emit carries the kept surface back into the corpus.
    with store.pool.connection() as conn:
        (text,) = _one(
            conn,
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1 "
            "AND chunk_kind = 'card_combined'",
            (winner,),
        )
    assert " (aka " in text and "TOF" in text


def test_fold_skips_when_the_nodes_share_no_surface(seeded: Store) -> None:
    """A `turnover-frequency` node about a different quantity must not
    absorb the seeded `tof` node just because the slugs line up."""
    store = seeded
    loser = _seeded_tof_id(store)
    winner = _put_winner(store, aliases=["catalyst turnover"])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{aliases}', '[]'::jsonb) "
            "WHERE ref_id = %s",
            (loser,),
        )
    _run_migration(_FOLD)
    assert not _loser_retired(store, loser)
    assert sorted(_claimants(store, "tof")) == [loser]


def test_fold_migrates_the_losers_edges(seeded: Store) -> None:
    store = seeded
    loser = _seeded_tof_id(store)
    winner = _put_winner(store, aliases=["TOF"])
    concept = store.insert_ref(kind="concept", slug=None, title="kinetics", meta={}).id
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, 'see-also', 'system')",
            (concept, loser),
        )
        (before_winner_edges,) = _one(
            conn,
            "SELECT count(*) FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (winner, winner),
        )
    _run_migration(_FOLD)
    with store.pool.connection() as conn:
        (migrated,) = _one(
            conn,
            "SELECT count(*) FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'see-also'",
            (concept, winner),
        )
        (left_on_loser,) = _one(
            conn,
            "SELECT count(*) FROM links WHERE (src_ref_id = %s OR dst_ref_id = %s) "
            "AND NOT (src_ref_id = %s AND dst_ref_id = %s AND relation = 'supersedes')",
            (loser, loser, winner, loser),
        )
        (winner_edges,) = _one(
            conn,
            "SELECT count(*) FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (winner, winner),
        )
    # The see-also moved; the loser's own specialises edge collapsed onto
    # the winner's pre-existing one (unique-endpoint dedup, no twin); the
    # winner gained the see-also copy and the supersedes trail.
    assert migrated == 1 and left_on_loser == 0
    assert winner_edges == before_winner_edges + 2


def _measure_on(store: Store, *, subject: int, measurand: int) -> int:
    with store.pool.connection() as conn:
        (mid,) = _one(
            conn,
            "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
            " value_num, reported_unit, run_key, actor) "
            "VALUES (%s, %s, '1', 1.0, '1/s', '', 'agent') RETURNING id",
            (subject, measurand),
        )
    return int(mid)


def _material(store: Store, slug: str) -> int:
    return store.insert_ref(kind="material", slug=slug, title=slug, meta={}).id


def test_fold_repoints_measures_under_the_matched_contract(seeded: Store) -> None:
    store = seeded
    loser = _seeded_tof_id(store)
    winner = _put_winner(store, aliases=["TOF"])
    mat = _material(store, "catalyst-a")
    measure = _measure_on(store, subject=mat, measurand=loser)
    _run_migration(_FOLD)
    with store.pool.connection() as conn:
        (measurand,) = _one(
            conn, "SELECT measurand_ref_id FROM measures WHERE id = %s", (measure,)
        )
    assert int(measurand) == winner
    assert _loser_retired(store, loser)


def test_fold_skips_when_the_contract_mismatches_with_measures(seeded: Store) -> None:
    store = seeded
    loser = _seeded_tof_id(store)
    # Same surface claimant, different unit contract: the numbers must
    # not be silently re-labelled onto it.
    winner = _put_winner(store, aliases=["TOF"], canonical_unit="Hz")
    mat = _material(store, "catalyst-b")
    measure = _measure_on(store, subject=mat, measurand=loser)
    _run_migration(_FOLD)
    assert not _loser_retired(store, loser)
    assert sorted(_claimants(store, "tof")) == sorted([loser, winner])
    with store.pool.connection() as conn:
        (measurand,) = _one(
            conn, "SELECT measurand_ref_id FROM measures WHERE id = %s", (measure,)
        )
    assert int(measurand) == loser


def test_fold_skips_under_a_current_review_pin(seeded: Store) -> None:
    store = seeded
    loser = _seeded_tof_id(store)
    _put_winner(store, aliases=["TOF"])
    mat = _material(store, "catalyst-c")
    measure = _measure_on(store, subject=mat, measurand=loser)
    with store.pool.connection() as conn:
        (sha,) = _one(conn, "SELECT precis_measure_sha(%s)", (measure,))
        conn.execute(
            "INSERT INTO reviews (target_kind, target_id, actor, content_sha, "
            " verdict, sha_basis) VALUES ('measure', %s, 'agent', %s, "
            " 'approved', 'content')",
            (measure, sha),
        )
    _run_migration(_FOLD)
    assert not _loser_retired(store, loser)


def test_fold_is_a_noop_without_the_winner(seeded: Store) -> None:
    """A fresh replay has no hand-put `turnover-frequency`: the seeded
    `tof` node stands alone, and the fold must not touch it."""
    store = seeded
    loser = _seeded_tof_id(store)
    _run_migration(_FOLD)
    assert _claimants(store, "tof") == [loser]
    assert not _loser_retired(store, loser)


def test_fold_is_a_noop_without_the_measurand_root(store: Store) -> None:
    """A database whose taxonomy is gone inserts nothing and retires
    nothing — the resolution guards fire on an empty scope."""
    _run_migration(_FOLD)
    with store.pool.connection() as conn:
        (n,) = _one(conn, "SELECT count(*) FROM refs WHERE kind = 'taxon'")
        (m,) = _one(conn, "SELECT count(*) FROM refs WHERE retired_at IS NOT NULL")
    assert n == 0 and m == 0
