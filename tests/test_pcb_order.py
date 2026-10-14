"""``put(args={'op':'order'})`` — the JLCPCB order pipeline's guards.

Every test substitutes a recording fake for
:class:`precis.pcb.jlc_order.JlcOrderClient` — no test here, or ever,
reaches the live endpoints (the rules in docs/backlog/pcb-jlc-order.md:
dry_run default, ``confirm='add-to-cart'`` verbatim, create = the
unpaid-order terminal step).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import jlc_order as pcb_jlc_order

_TWO_PAD = {
    "pads": [
        {
            "number": "1",
            "shape": "RECT",
            "x": -0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "2",
            "shape": "RECT",
            "x": 0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
    ],
    "pin_map": {"1": {"name": "1", "tags": []}, "2": {"name": "2", "tags": []}},
}

_DESIGN = {
    "components": [
        {
            "refdes": "C1",
            "label": "100nF 0402",
            "part": "C1525",
            "footprint": "0402",
            "x": 10.0,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
        {
            "refdes": "R1",
            "label": "4.7k 0402",
            "part": "C25900",
            "footprint": "0402",
            "x": 12.0,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [{"name": "VCC3V3", "class": "power"}, {"name": "GND", "class": "gnd"}],
    "connections": [
        {"net": "VCC3V3", "refdes": "C1", "pin": "1"},
        {"net": "VCC3V3", "refdes": "R1", "pin": "1"},
        {"net": "GND", "refdes": "C1", "pin": "2"},
        {"net": "GND", "refdes": "R1", "pin": "2"},
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 15], [0, 15]]}}
    ],
}

_ADDRESS = {
    "firstName": "A",
    "lastName": "B",
    "streetAddress": "1 Main St",
    "city": "Zurich",
    "country": "CH",
    "postalCode": "8000",
    "cellOrMobileNumber": "+41000000000",
}


class _FakeOrderClient:
    """Records every call; a ``create`` that fires in a dry-run test is a
    test failure by assertion, not a near-miss."""

    def __init__(self, *, available: bool = True, quote_raises=None):
        self.available = available
        self._quote_raises = quote_raises
        self.calls: list[tuple] = []

    def quote(self, request):
        self.calls.append(("quote", dict(request)))
        if self._quote_raises is not None:
            raise self._quote_raises
        return {"code": 200, "data": {"totalPrice": "12.34", "currency": "USD"}}

    def upload_gerber(self, filename, blob, *, meta=None):
        self.calls.append(("upload", filename, len(blob)))
        return "fk-test-1"

    def create_order(self, request, *, confirm):
        self.calls.append(("create", dict(request), confirm))
        return {"code": 200, "data": {"orderId": 991, "batchNum": "B-TEST"}}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


@pytest.fixture
def seeded(pcb):
    pcb.put(id="order-board", args=_DESIGN)
    pcb.store.part_footprint_put("C1525", _TWO_PAD)
    pcb.store.part_footprint_put("C25900", _TWO_PAD)
    return "order-board"


def _install(monkeypatch, client):
    monkeypatch.setattr(pcb_jlc_order, "JlcOrderClient", lambda **kw: client)


def _board_meta(pcb, slug="order-board"):
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    board = pcb.store.pcb_load(ref.id)["board"]
    return dict(board["meta"])


# ── dry run ────────────────────────────────────────────────────────────


def test_order_dry_run_default_builds_payload_and_quotes(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    resp = pcb.put(id=seeded, args={"op": "order"})
    body = resp.body
    assert "DRY RUN" in body
    assert '"layer": 4' in body and '"width": 20.0' in body
    assert '"length": 15.0' in body and '"qty": 5' in body
    assert "fileKey" in body  # placeholder, not a real key
    assert "add-to-cart" in body  # the confirm incantation is shown
    # the quote DID run (read-only), upload and create did not
    assert [c[0] for c in fake.calls] == ["quote"]
    assert fake.calls[0][1]["pcbParam"]["layer"] == 4
    assert _board_meta(pcb) == {}  # nothing recorded on a dry run


def test_order_dry_run_overrides_and_qty(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    resp = pcb.put(
        id=seeded,
        args={
            "op": "order",
            "qty": 10,
            "thickness_mm": 1.0,
            "pcb_param": {"surfaceFinish": 2, "pcbColor": 1},
        },
    )
    assert '"qty": 10' in resp.body and '"surfaceFinish": 2' in resp.body


def test_order_dry_run_survives_a_quote_failure(pcb, seeded, monkeypatch):
    from precis.pcb._http import VendorUnavailable

    fake = _FakeOrderClient(quote_raises=VendorUnavailable("jlcpcb", "500"))
    _install(monkeypatch, fake)
    resp = pcb.put(id=seeded, args={"op": "order"})
    assert "DRY RUN" in resp.body
    assert "quote: unavailable" in resp.body
    assert [c[0] for c in fake.calls] == ["quote"]


def test_order_dry_run_without_credentials_skips_quote(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient(available=False)
    _install(monkeypatch, fake)
    resp = pcb.put(id=seeded, args={"op": "order"})
    assert "DRY RUN" in resp.body
    assert "credentials do not resolve" in resp.body
    assert fake.calls == []


def test_order_dry_run_string_ambiguity_stays_dry(pcb, seeded, monkeypatch):
    """A typo'd dry_run (or a bare confirm without dry_run=False) must
    still not reach create."""
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    resp = pcb.put(
        id=seeded,
        args={
            "op": "order",
            "dry_run": "yes",
            "confirm": "add-to-cart",
            "shipping_address": dict(_ADDRESS),
        },
    )
    assert "DRY RUN" in resp.body
    assert "create" not in [c[0] for c in fake.calls]


# ── the guarded live path ──────────────────────────────────────────────


def test_order_live_requires_confirm_verbatim(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    with pytest.raises(BadInput, match="add-to-cart"):
        pcb.put(
            id=seeded,
            args={
                "op": "order",
                "dry_run": False,
                "shipping_address": dict(_ADDRESS),
            },
        )
    with pytest.raises(BadInput, match="add-to-cart"):
        pcb.put(
            id=seeded,
            args={
                "op": "order",
                "dry_run": False,
                "confirm": "yes",
                "shipping_address": dict(_ADDRESS),
            },
        )
    assert fake.calls == []


def test_order_live_requires_shipping_address(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    with pytest.raises(BadInput, match="shipping_address"):
        pcb.put(
            id=seeded,
            args={"op": "order", "dry_run": False, "confirm": "add-to-cart"},
        )
    assert fake.calls == []


def test_order_live_requires_credentials(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient(available=False)
    _install(monkeypatch, fake)
    with pytest.raises(BadInput, match="credentials"):
        pcb.put(
            id=seeded,
            args={
                "op": "order",
                "dry_run": False,
                "confirm": "add-to-cart",
                "shipping_address": dict(_ADDRESS),
            },
        )
    assert fake.calls == []


def test_order_live_uploads_quotes_creates_and_records_meta(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    resp = pcb.put(
        id=seeded,
        args={
            "op": "order",
            "dry_run": False,
            "confirm": "add-to-cart",
            "qty": 10,
            "shipping_address": dict(_ADDRESS),
            "shipping_method": "std",
        },
    )
    assert "ADDED TO CART" in resp.body
    assert "B-TEST" in resp.body
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["upload", "quote", "create"]
    # the create payload carried the uploaded fileKey + the address
    create = fake.calls[2][1]
    assert create["fileKey"] == "fk-test-1"
    assert create["shippingAddress"]["country"] == "CH"
    assert fake.calls[2][2] == "add-to-cart"
    meta = _board_meta(pcb)["jlc_order"]
    assert meta["batch_num"] == "B-TEST"
    assert meta["order_id"] == "991"
    assert meta["file_key"] == "fk-test-1"
    assert meta["state"] == "cart"


def test_order_second_create_needs_reorder(pcb, seeded, monkeypatch):
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    live = {
        "op": "order",
        "dry_run": False,
        "confirm": "add-to-cart",
        "shipping_address": dict(_ADDRESS),
    }
    pcb.put(id=seeded, args=live)
    with pytest.raises(BadInput, match="reorder"):
        pcb.put(id=seeded, args=dict(live))
    assert [c[0] for c in fake.calls] == ["upload", "quote", "create"]
    resp = pcb.put(id=seeded, args={**live, "reorder": True})
    assert "ADDED TO CART" in resp.body
    assert [c[0] for c in fake.calls].count("create") == 2


def test_order_refuses_a_synthesized_pad_board(pcb, monkeypatch):
    """The same SynthesizedPadError boundary view='gerber' holds — an
    order built on land-pattern bounds must not upload."""
    fake = _FakeOrderClient()
    _install(monkeypatch, fake)
    pcb.put(
        id="no-fp-board",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "mystery",
                    "part": "C999999",
                    "footprint": "???",
                    "x": 5.0,
                    "y": 5.0,
                    "pins": [{"name": "1"}],
                }
            ],
            "features": [
                {
                    "ftype": "outline",
                    "geom": {"path": [[0, 0], [10, 0], [10, 10], [0, 10]]},
                }
            ],
        },
    )
    with pytest.raises(BadInput, match="synthesized"):
        pcb.put(id="no-fp-board", args={"op": "order"})
    assert fake.calls == []
