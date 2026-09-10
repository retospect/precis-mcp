"""``nanopub_stale_sweep`` — gr279770's scheduled re-gate/auto-demote pass.

DB-backed: real hub + real ``candidate`` row (``store.
nanopub_create_publish_row``, the same construction
``tests/workers/test_health_digest.py``'s sibling stale-detection tests
use), no LLM.
"""

from __future__ import annotations

from typing import Any

from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.workers.nanopub_stale_sweep import (
    META_STALE_DEMOTED,
    run_nanopub_stale_sweep_pass,
)
from tests.workers._helpers import seed_ref


def _staged(store: Any, sentence: str) -> tuple[int, int]:
    """A hub with a live ``candidate`` row — ``(hub_ref_id, publish_row_id)``."""
    hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    row = store.nanopub_create_publish_row(hub, artifact_type="claim")
    return hub, row.id


def test_sweep_leaves_a_clean_candidate_alone(store: Any) -> None:
    hub, row_id = _staged(store, "DFT calculations show that graphene is stiff.")

    result = run_nanopub_stale_sweep_pass(store)

    assert result.claimed == 1
    assert result.ok == 0
    assert result.failed == 0
    row = store.nanopub_publish_row(hub)
    assert row is not None
    assert row.id == row_id
    assert row.state == "candidate"
    hub_ref = store.fetch_refs_by_ids([hub])[hub]
    assert META_STALE_DEMOTED not in hub_ref.meta


def test_sweep_demotes_a_lint_blocked_candidate(store: Any) -> None:
    """Same sentence ``test_health_digest.py::
    test_nanopub_candidates_fresh_flags_lint_blocked_row`` uses — missing
    terminal period, a blocking lint code."""
    hub, row_id = _staged(store, "Graphene is the strongest material ever measured")

    result = run_nanopub_stale_sweep_pass(store)

    assert result.claimed == 1
    assert result.ok == 1
    assert store.nanopub_publish_row(hub) is None

    hub_ref = store.fetch_refs_by_ids([hub])[hub]
    demoted = hub_ref.meta.get(META_STALE_DEMOTED)
    assert demoted is not None
    assert demoted["publish_id"] == row_id
    assert demoted["reason_kind"] == "lint"
    assert "no-terminal-period" in demoted["reason_label"]


def test_sweep_demotes_a_disputed_candidate(store: Any) -> None:
    hub, row_id = _staged(store, "DFT calculations show that graphene is stiff.")
    other = seed_ref(store, title="opposing paper", kind="paper")
    store.add_link(
        src_ref_id=other,
        dst_ref_id=hub,
        relation="contradicts",
        set_by="agent",
    )

    result = run_nanopub_stale_sweep_pass(store)

    assert result.ok == 1
    assert store.nanopub_publish_row(hub) is None
    hub_ref = store.fetch_refs_by_ids([hub])[hub]
    demoted = hub_ref.meta[META_STALE_DEMOTED]
    assert demoted["publish_id"] == row_id
    assert demoted["reason_kind"] == "disputed"


def test_sweep_demotes_a_noncanonical_candidate(store: Any) -> None:
    """A staged row whose hub lost ``STATUS:canonical`` (a chase-tree
    finding, not a strict claim hub) is stale regardless of lint — the
    same case ``health_digest``'s check flags."""
    hub, _row_id = _staged(store, "DFT calculations show that graphene is stiff.")
    store.remove_tag(hub, Tag.closed("STATUS", "canonical"))

    result = run_nanopub_stale_sweep_pass(store)

    assert result.ok == 1
    assert store.nanopub_publish_row(hub) is None
    hub_ref = store.fetch_refs_by_ids([hub])[hub]
    assert hub_ref.meta[META_STALE_DEMOTED]["reason_kind"] == "noncanonical"


def test_sweep_is_idempotent_on_a_clean_corpus(store: Any) -> None:
    _staged(store, "DFT calculations show that graphene is stiff.")

    first = run_nanopub_stale_sweep_pass(store)
    second = run_nanopub_stale_sweep_pass(store)

    assert (first.claimed, first.ok, first.failed) == (1, 0, 0)
    assert (second.claimed, second.ok, second.failed) == (1, 0, 0)


def test_sweep_re_run_after_a_demotion_is_a_no_op(store: Any) -> None:
    """The demoted row is gone from ``state='candidate'`` — nothing left
    for the next fire to re-act on."""
    hub, _row_id = _staged(store, "Graphene is the strongest material ever measured")

    first = run_nanopub_stale_sweep_pass(store)
    second = run_nanopub_stale_sweep_pass(store)

    assert first.ok == 1
    assert second.claimed == 0
    assert second.ok == 0
    assert store.nanopub_publish_row(hub) is None


def test_discarded_candidate_restages_through_the_normal_path(store: Any) -> None:
    """Discarded, never tombstoned: the hub has no live publish row after
    the sweep, so the ordinary staging primitive (:meth:`Store.
    nanopub_create_publish_row`, what ``mint.approve`` calls when a hub has
    no row) freely mints a fresh ``candidate`` — no unique-index collision,
    no manual unblock."""
    hub, old_row_id = _staged(store, "Graphene is the strongest material ever measured")

    run_nanopub_stale_sweep_pass(store)
    assert store.nanopub_publish_row(hub) is None

    new_row = store.nanopub_create_publish_row(hub, artifact_type="claim")

    assert new_row.id != old_row_id
    assert new_row.state == "candidate"
