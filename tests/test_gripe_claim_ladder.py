"""Gripe status ladder (gr541800, td541978): the ``claimed-by:`` lease
drives STATUS.

open → triaged → fixing (claim) → verify (on main, ``resolved: <sha>``)
→ done (sha on origin/prod) | wontfix. Claiming writes ``fixing`` under
the todo lease's CAS guard; dropping the lease without a ``resolved:``
comment returns the gripe to ``triaged``; ``verify`` needs a newest
``resolved:`` comment, like the terminals, and releases the lease.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.store.types import Tag

pytestmark = pytest.mark.db


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _new(gripe: GripeHandler, status: str = "triaged") -> int:
    gripe.put(text=f"ladder probe {status}")
    gid = int(gripe.store.list_refs(kind="gripe", limit=1)[0].id)
    if status != "open":
        gripe.store.add_tag(
            gid, Tag.closed("STATUS", status), replace_prefix=True, set_by="agent"
        )
    return gid


def _status(gripe: GripeHandler, gid: int) -> str:
    vals = [str(t) for t in gripe.store.tags_for(gid) if str(t).startswith("STATUS:")]
    assert len(vals) == 1, vals
    return vals[0].split(":", 1)[1]


def _claims(gripe: GripeHandler, gid: int) -> list[tuple[str, object]]:
    with gripe.store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value, rt.expires_at FROM ref_tags rt"
            " JOIN tags t ON t.tag_id = rt.tag_id"
            " WHERE rt.ref_id = %s AND t.namespace = 'OPEN'"
            "   AND t.value LIKE 'claimed-by:%%'",
            (gid,),
        ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]


def test_claim_writes_fixing_with_a_lease(gripe: GripeHandler) -> None:
    gid = _new(gripe, "triaged")
    resp = gripe.tag(id=gid, add=["claimed-by:builder-a"])
    assert _status(gripe, gid) == "fixing"
    assert "STATUS:fixing" in resp.body
    [(value, expires)] = _claims(gripe, gid)
    assert value == "claimed-by:builder-a" and expires is not None


def test_claim_on_open_gripe_also_fixing(gripe: GripeHandler) -> None:
    gid = _new(gripe, "open")
    gripe.tag(id=gid, add=["claimed-by:builder-a"])
    assert _status(gripe, gid) == "fixing"


def test_explicit_status_wins_over_claim(gripe: GripeHandler) -> None:
    gid = _new(gripe, "open")
    gripe.tag(id=gid, add=["claimed-by:builder-a", "STATUS:triaged"])
    assert _status(gripe, gid) == "triaged"


def test_claim_on_verify_gripe_keeps_verify(gripe: GripeHandler) -> None:
    gid = _new(gripe, "verify")
    gripe.tag(id=gid, add=["claimed-by:builder-a"])
    assert _status(gripe, gid) == "verify"


def test_second_claimer_refused_while_lease_live(
    gripe: GripeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _new(gripe, "triaged")
    gripe.tag(id=gid, add=["claimed-by:builder-a"])
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker-b")
    with pytest.raises(BadInput, match="gripe id=.* already claimed by"):
        gripe.tag(id=gid, add=["claimed-by:builder-b"])
    assert [v for v, _ in _claims(gripe, gid)] == ["claimed-by:builder-a"]


def test_lease_drop_without_resolved_returns_to_triaged(gripe: GripeHandler) -> None:
    gid = _new(gripe, "triaged")
    gripe.tag(id=gid, add=["claimed-by:builder-a"])
    gripe.put(id=gid, text="gave up: needs a design call")
    resp = gripe.tag(id=gid, remove=["claimed-by:builder-a"])
    assert _status(gripe, gid) == "triaged"
    assert "STATUS:triaged" in resp.body
    assert _claims(gripe, gid) == []


def test_lease_drop_after_resolved_keeps_fixing(gripe: GripeHandler) -> None:
    gid = _new(gripe, "triaged")
    gripe.tag(id=gid, add=["claimed-by:builder-a"])
    gripe.put(id=gid, text="resolved: abc1234 (main, release pending) — test")
    gripe.tag(id=gid, remove=["claimed-by:builder-a"])
    assert _status(gripe, gid) == "fixing"


def test_verify_requires_resolved_comment(gripe: GripeHandler) -> None:
    gid = _new(gripe, "fixing")
    with pytest.raises(BadInput, match="resolved:") as exc:
        gripe.tag(id=gid, add=["STATUS:verify"])
    assert "release pending" in (exc.value.next or "")
    assert _status(gripe, gid) == "fixing"


def test_verify_with_resolved_releases_lease(gripe: GripeHandler) -> None:
    gid = _new(gripe, "triaged")
    gripe.tag(id=gid, add=["claimed-by:builder-a"])
    gripe.put(
        id=gid,
        text="resolved: abc1234 (main, release pending) — unit test",
        tags=["STATUS:verify"],
    )
    assert _status(gripe, gid) == "verify"
    assert _claims(gripe, gid) == []
