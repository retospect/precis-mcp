"""Provenance check for the kinetics-v2 TPD reference dataset (spec D11).

Every value in ``tests/fixtures/kinetics/tpd_reference.json`` cites a paper
chunk handle (``pc…``). This test loads the committed chunk snapshot
(``tpd_reference_chunks.json``, exported read-only from prod so CI needs
no prod access) and asserts that each cited chunk exists and that its
text contains the ``printed`` substring after normalisation — the
``printed`` field is the verbatim evidence for the interpreted ``value``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "kinetics"

_MINUS_VARIANTS = dict.fromkeys(map(ord, "−–—‑‒‐"), "-")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_MINUS_VARIANTS)
    text = _TAG_RE.sub("", text)
    text = text.replace("×", "x").replace("**", "").replace("*", "")
    text = text.replace("“", '"').replace("”", '"').replace("„", '"')
    return _WS_RE.sub("", text)


def _load() -> tuple[dict, dict]:
    table = json.loads((FIXTURE_DIR / "tpd_reference.json").read_text(encoding="utf-8"))
    snapshot = json.loads(
        (FIXTURE_DIR / "tpd_reference_chunks.json").read_text(encoding="utf-8")
    )
    return table, snapshot["chunks"]


def test_every_value_cites_an_existing_chunk() -> None:
    table, chunks = _load()
    missing = [
        (m["id"], name, v["chunk"])
        for m in table["measurements"]
        for name, v in m["values"].items()
        if v["chunk"] not in chunks
    ]
    assert not missing, f"cited chunks missing from snapshot: {missing}"


def test_printed_string_appears_in_cited_chunk() -> None:
    table, chunks = _load()
    failures = []
    for m in table["measurements"]:
        for name, v in m["values"].items():
            chunk_text = _normalise(chunks[v["chunk"]])
            if _normalise(v["printed"]) not in chunk_text:
                failures.append((m["id"], name, v["printed"], v["chunk"]))
    assert not failures, f"printed strings not found in cited chunks: {failures}"


def test_row_shape() -> None:
    table, _ = _load()
    assert table["measurements"], "empty reference set"
    for m in table["measurements"]:
        assert re.fullmatch(r"pa\d+", m["paper"]), m["id"]
        assert m["adsorbate"] and m["surface"], m["id"]
        assert m["values"], m["id"]
        for name, v in m["values"].items():
            assert re.fullmatch(r"pc\d+", v["chunk"]), (m["id"], name)
            assert v["printed"], (m["id"], name)
