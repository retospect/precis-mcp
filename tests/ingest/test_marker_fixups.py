"""Domain-aware surya OCR prompt context (gr514187): the paper's
title/abstract ride into the VLM OCR prompt with a hedging directive so
figure ``<img alt>`` descriptions stop inventing chemistry."""

from __future__ import annotations

import sys
import types

import pytest

from precis.ingest.marker import (
    _apply_surya_ocr_context,
    _figure_ocr_prompt,
    _restore_surya_prompts,
    ocr_context,
)


def test_ocr_context_from_title_and_abstract() -> None:
    ctx = ocr_context("Adsorption of Atomic Oxygen on Pt(111)", "We fit a CE.")
    assert ctx is not None
    assert "Pt(111)" in ctx and "We fit a CE." in ctx
    assert ocr_context("", "") is None


def test_figure_ocr_prompt_carries_context() -> None:
    prompt = _figure_ocr_prompt("OCR this image to HTML.", "O on Pt(111) paper")
    assert prompt.startswith("OCR this image to HTML.")
    assert "O on Pt(111) paper" in prompt
    assert "appears to be" in prompt  # hedging directive


def _fake_surya(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    prompts = types.SimpleNamespace(
        PROMPT_MAPPING={
            "block": "OCR this block image to HTML.",
            "high_accuracy_bbox": "OCR this image to HTML.",
            "layout": "LAYOUT",
            "table_rec": "TABLEREC",
        }
    )
    surya = types.ModuleType("surya")
    inference = types.ModuleType("surya.inference")
    monkeypatch.setattr(inference, "prompts", prompts, raising=False)
    monkeypatch.setattr(surya, "inference", inference, raising=False)
    monkeypatch.setitem(sys.modules, "surya", surya)
    monkeypatch.setitem(sys.modules, "surya.inference", inference)
    monkeypatch.setitem(sys.modules, "surya.inference.prompts", prompts)
    return prompts


def test_prompt_map_patched_and_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _fake_surya(monkeypatch)
    saved = _apply_surya_ocr_context("Cluster expansions on Pt(111)")
    assert "Pt(111)" in prompts.PROMPT_MAPPING["block"]
    assert "Pt(111)" in prompts.PROMPT_MAPPING["high_accuracy_bbox"]
    # JSON-schema prompts are never touched.
    assert prompts.PROMPT_MAPPING["layout"] == "LAYOUT"
    assert prompts.PROMPT_MAPPING["table_rec"] == "TABLEREC"
    _restore_surya_prompts(saved)
    assert prompts.PROMPT_MAPPING["block"] == "OCR this block image to HTML."


def test_no_context_leaves_prompts_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _fake_surya(monkeypatch)
    saved = _apply_surya_ocr_context(None)
    assert saved == {}
    assert prompts.PROMPT_MAPPING["block"] == "OCR this block image to HTML."
