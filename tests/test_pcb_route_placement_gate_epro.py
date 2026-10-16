"""gr451274: the pre-route placement DRC gate against a real imported board
(tests/fixtures/pcb_epro_tiny), and its ``force=True`` override."""

from __future__ import annotations

import io
import pathlib
import zipfile
from typing import Any, cast

import pytest

from precis.ingest import pcb_epro
from precis.pcb import epro
from precis.store import Store
from precis.workers.job_types import pcb_route

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("project2.json", "board.epru"):
            zf.writestr(name, (_FIXTURE / name).read_bytes())
    return buf.getvalue()


class _FakeCtx:
    def __init__(self, store: Store, *, params: dict[str, Any]) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="pcb_route gate test",
            meta={"executor": "job_inproc", "job_type": "pcb_route"},
        )
        self.ref_id = int(ref.id)
        self.title = "pcb_route gate test"
        self.meta: dict[str, Any] = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:
        pass

    def set_meta(self, **_kw: Any) -> None:
        pass

    def is_cancel_requested(self) -> bool:
        return False


def _import(store: Store, slug: str) -> int:
    return int(
        pcb_epro.import_epro(store, _zip(), slug=slug, title="T", copper="none").ref_id
    )


@pytest.fixture
def imported(store: Store, monkeypatch: pytest.MonkeyPatch) -> int:
    """The tiny board with its R0603 pad 3 moved clear of pads 1 and 2.

    As shipped, that synthetic third pad sits at (0, -0.635) and overlaps
    both real pads, so the unmodified board is itself illegal (pinned by
    ``test_fixture_r0603_third_pad_is_refused_as_shipped``)."""
    build = epro.build_design

    def _clear_pad3(*a: Any, **kw: Any) -> Any:
        out = build(*a, **kw)
        for fp in out[0].footprints:
            if fp["name"] == "R0603":
                for p in fp["pads"]:
                    if p["pin"] == "3":
                        p["y"] = -2.0
        return out

    monkeypatch.setattr(epro, "build_design", _clear_pad3)
    return _import(store, "gate-epro")


def _overlap_two_parts(store: Store, ref_id: int) -> tuple[str, str]:
    """Stack the first two parts at one point, both pinned."""
    a, b = [str(i["refdes"]) for i in store.pcb_graph(ref_id)["instances"]][:2]
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT x, y FROM pcb_instances WHERE ref_id=%s AND refdes=%s "
            "AND retired_at IS NULL",
            (ref_id, a),
        ).fetchone()
        assert row is not None
        ax, ay = row
        conn.execute(
            "UPDATE pcb_instances SET x=%s, y=%s, fixed='both' "
            "WHERE ref_id=%s AND refdes=%s AND retired_at IS NULL",
            (ax, ay, ref_id, b),
        )
        conn.execute(
            "UPDATE pcb_instances SET fixed='both' "
            "WHERE ref_id=%s AND refdes=%s AND retired_at IS NULL",
            (ref_id, a),
        )
        conn.commit()
    return a, b


def _copper_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pcb_copper c JOIN pcb_boards b "
            "ON b.board_id = c.board_id WHERE b.ref_id = %s",
            (ref_id,),
        ).fetchone()
        assert row is not None
        return int(row[0])


def _pin_all(store: Store, ref_id: int) -> None:
    """Hold every part at its imported pose: the anneal can pull a movable
    R1 onto U1's lands (SIG wirelength beats the courtyard term on this
    board), which would test the anneal, not the gate."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_instances SET fixed='both' "
            "WHERE ref_id=%s AND retired_at IS NULL",
            (ref_id,),
        )
        conn.commit()


def _run(store: Store, ref_id: int, **extra: Any) -> _FakeCtx:
    ctx = _FakeCtx(
        store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1, **extra}
    )
    pcb_route._dispatch(cast(Any, ctx), pcb_route.SPEC)
    return ctx


def test_overlapping_pinned_parts_are_refused_and_nothing_written(
    store: Store, imported: int
) -> None:
    a, b = _overlap_two_parts(store, imported)
    ctx = _run(store, imported)

    assert len(ctx.failures) == 1
    reason, failure_class = ctx.failures[0]
    assert failure_class == "input"
    assert "refusing to route" in reason
    assert "clearance" in reason, reason
    assert a in reason and b in reason
    assert "force=True" in reason
    # gr489913: every part is pinned, so nothing moved — the stored
    # placement is the illegal one, and the fixed hint names the pair.
    assert "the placement is already illegal" in reason
    assert "ANNEALED" not in reason
    assert "cannot be auto-shifted" in reason
    assert not ctx.summaries
    assert all(r["status"] != "realized" for r in store.pcb_route_status(imported))
    assert _copper_count(store, imported) == 0


def test_fixture_r0603_third_pad_is_refused_as_shipped(store: Store) -> None:
    ref_id = _import(store, "gate-epro-raw")
    ctx = _run(store, ref_id)

    assert len(ctx.failures) == 1
    reason, failure_class = ctx.failures[0]
    assert failure_class == "input"
    assert "pad[R1/1 net SIG] <-> pad[R1/3 no net]" in reason, reason
    assert _copper_count(store, ref_id) == 0


def test_clean_imported_placement_passes_the_gate(store: Store, imported: int) -> None:
    _pin_all(store, imported)
    ctx = _run(store, imported)

    assert not any(
        fc == "input" and "refusing to route" in r for r, fc in ctx.failures
    ), "\n".join(r for r, _ in ctx.failures)
    text = "\n".join(t for _, t in ctx.summaries)
    assert "routed" in text
    assert "0 pre-route DRC error(s)" in text
    assert "OVERRIDDEN" not in text


def test_force_routes_an_illegal_placement_and_records_the_override(
    store: Store, imported: int
) -> None:
    a, b = _overlap_two_parts(store, imported)
    ctx = _run(store, imported, force=True)

    assert not any("refusing to route" in r for r, _ in ctx.failures)
    text = "\n".join(t for _, t in ctx.summaries)
    assert "OVERRIDDEN (force=True)" in text
    assert "clearance" in text
    assert a in text and b in text
    assert "routed" in text
