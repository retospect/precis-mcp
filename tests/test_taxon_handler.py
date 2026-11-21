"""gr676966: ``put(kind='taxon', title=…, body=…)`` spells
``text='<name> — <definition>'``.

The put verb's schema declares ``title=``/``body=`` for every kind; the
taxon handler now reads them instead of refusing. Handler tests run
against real PG (the ``store`` fixture) like tests/test_taxon.py.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.errors import BadInput


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.taxon import TaxonHandler

    return TaxonHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\btn(\d+)\b", resp.body)
    assert m is not None, f"no taxon handle in ack: {resp.body!r}"
    return int(m.group(1))


def test_put_title_body_equals_text(store: Any) -> None:
    """title= + body= mint the same node text='<name> — <definition>' does."""
    h = _handler(store)
    via_pair = store.get_ref(
        kind="taxon",
        id=_created_id(
            h.put(title="zxq coined term", body="a coined gloss for testing")
        ),
    )
    via_text = store.get_ref(
        kind="taxon",
        id=_created_id(
            h.put(
                text="zxq coined term — a coined gloss for testing",
                dedup=False,  # same name as the mint above: it IS the duplicate
            )
        ),
    )
    assert via_pair.title == via_text.title == "zxq coined term"
    for key in ("name", "norm_name", "slug", "definition", "status"):
        assert via_pair.meta[key] == via_text.meta[key]


def test_put_title_without_definition_names_text_form(store: Any) -> None:
    """title= alone is refused — a taxon needs its definition — and the
    refusal's next names the text= form."""
    with pytest.raises(BadInput) as ei:
        _handler(store).put(title="zxq bare term")
    assert ei.value.next == "text='<name> — <definition>'"


def test_put_title_with_text_refused_ambiguous(store: Any) -> None:
    with pytest.raises(BadInput, match="ambiguous"):
        _handler(store).put(text="zxq term — a gloss", title="zxq term")


def test_put_title_body_through_the_verb(store: Any, runtime_with_store: Any) -> None:
    """The reported path: ``put(kind='taxon', title=…, body=…)`` over the
    MCP verb — title= must pass the strictness gate and body= must not be
    aliased onto text=."""
    from precis.tools import core

    core._runtime = runtime_with_store
    try:
        out = core.put(kind="taxon", title="zxq verb term", body="a verb gloss")
    finally:
        core._runtime = None
    content = getattr(out, "content", None)
    body = content[0].text if content else str(out)
    assert "[error" not in body, body
    ref = store.get_ref(kind="taxon", id=_created_id_str(body))
    assert ref.meta["name"] == "zxq verb term"
    assert ref.meta["definition"] == "a verb gloss"


def _created_id_str(body: str) -> int:
    m = re.search(r"\btn(\d+)\b", body)
    assert m is not None, f"no taxon handle in ack: {body!r}"
    return int(m.group(1))
