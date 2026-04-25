"""Phase F — oracle ingest CLI tests.

Dry-run-only.  No DB needed.  Live-store ingest is exercised in the
acatome-store integration suite once a Postgres+pgvector test
fixture is wired up.
"""

from __future__ import annotations

import yaml
import pytest

from precis.handlers.oracle_ingest import (
    _bundled_oracle_dir,
    _cli_main,
    _render_chunk_body,
    _section_path,
    ingest_directory,
    ingest_paper,
)


# ---------------------------------------------------------------------------
# Helper-fn unit tests
# ---------------------------------------------------------------------------


class TestRenderChunkBody:
    def test_body_only(self):
        entry = {"title": "Foo", "body": "  Hello world  "}
        assert _render_chunk_body(entry) == "Hello world"

    def test_body_with_tail(self):
        entry = {
            "body": "Knuth says no.",
            "source": "Knuth 1974",
            "lang": "en",
        }
        out = _render_chunk_body(entry)
        assert "Knuth says no." in out
        assert "_source_: Knuth 1974" in out
        assert "_lang_: en" in out

    def test_iching_tail_keys(self):
        entry = {
            "body": "Hexagram body",
            "original": "乾",
            "pinyin": "qián",
            "trigrams": "Heaven over Heaven",
            "binary": "111111",
        }
        out = _render_chunk_body(entry)
        assert "_original_: 乾" in out
        assert "_pinyin_: qián" in out
        assert "_trigrams_: Heaven over Heaven" in out
        assert "_binary_: 111111" in out

    def test_blank_keys_skipped(self):
        entry = {"body": "x", "source": "", "lang": None}
        out = _render_chunk_body(entry)
        assert out == "x"


class TestSectionPath:
    def test_title_only(self):
        assert _section_path({"title": "Foo"}) == ["Foo"]

    def test_with_extras(self):
        out = _section_path({
            "title": "Hexagram 12",
            "extra_section_path": ["Cognitive: Goodhart's Law (principle)"],
        })
        assert out == [
            "Hexagram 12",
            "Cognitive: Goodhart's Law (principle)",
        ]

    def test_dedup_and_em_dash_dropped(self):
        out = _section_path({
            "title": "Hexagram 12",
            "extra_section_path": ["Hexagram 12", "—"],
        })
        # Dup of title and em-dash filler both filtered out.
        assert out == ["Hexagram 12"]

    def test_empty_title(self):
        assert _section_path({}) == []


# ---------------------------------------------------------------------------
# Bundled-data discovery
# ---------------------------------------------------------------------------


class TestBundledData:
    def test_oracle_dir_locatable(self):
        d = _bundled_oracle_dir()
        assert d is not None
        assert d.is_dir()

    def test_expected_traditions_present(self):
        d = _bundled_oracle_dir()
        present = {p.stem for p in d.glob("*.yaml")}
        for required in (
            "iching", "chengyu", "stoic", "engineering",
            "proverbs-euro", "proverbs-irish", "talmudic", "zen",
        ):
            assert required in present, (
                f"missing oracle paper: {required}"
            )

    def test_iching_has_64_entries(self):
        d = _bundled_oracle_dir()
        with open(d / "iching.yaml") as f:
            doc = yaml.safe_load(f)
        assert doc["slug"] == "iching"
        assert len(doc["entries"]) == 64

    def test_every_tradition_has_required_fields(self):
        d = _bundled_oracle_dir()
        for yp in d.glob("*.yaml"):
            with open(yp) as f:
                doc = yaml.safe_load(f)
            assert "slug" in doc, f"{yp.name}: missing slug"
            assert "title" in doc, f"{yp.name}: missing title"
            assert "entries" in doc, f"{yp.name}: missing entries"
            for i, e in enumerate(doc["entries"]):
                assert "title" in e, f"{yp.name}[{i}]: missing title"
                assert "body" in e, f"{yp.name}[{i}]: missing body"


# ---------------------------------------------------------------------------
# Dry-run ingest
# ---------------------------------------------------------------------------


class TestDryRunIngest:
    def test_paper_dry_run_iching(self):
        d = _bundled_oracle_dir()
        stats = ingest_paper(d / "iching.yaml", dry_run=True)
        assert stats["created"] == 1
        assert stats["chunks"] == 64
        assert stats["errors"] == 0

    def test_directory_dry_run_full_set(self):
        d = _bundled_oracle_dir()
        agg = ingest_directory(d, dry_run=True)
        assert agg["errors"] == 0
        assert agg["files"] == 9
        # iching=64, buddhist=52, proverbs-euro=40, chengyu=29,
        # engineering=21, zen=18, stoic=15, talmudic=14, irish=3 = 256
        assert agg["chunks"] == 256

    def test_directory_dry_run_per_file(self):
        d = _bundled_oracle_dir()
        agg = ingest_directory(d, dry_run=True)
        per = agg["per_file"]
        # File order is sorted alphabetically.
        assert per["iching.yaml"]["chunks"] == 64
        assert per["chengyu.yaml"]["chunks"] == 29

    def test_custom_yaml(self, tmp_path):
        # Build a minimal custom paper and confirm it parses + writes.
        yaml_text = """
slug: test-tradition
title: Test Tradition
description: A tiny test paper.
tags: [test]
entries:
  - title: First entry
    body: |
      Body text.
    source: Test source
"""
        p = tmp_path / "test.yaml"
        p.write_text(yaml_text)
        stats = ingest_paper(p, dry_run=True)
        assert stats["created"] == 1
        assert stats["chunks"] == 1

    def test_missing_required_keys(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("title: foo\n")  # missing slug + entries
        with pytest.raises(ValueError):
            ingest_paper(bad, dry_run=True)

    def test_entries_must_be_list(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "slug: x\ntitle: y\nentries: 'not a list'\n"
        )
        with pytest.raises(ValueError):
            ingest_paper(bad, dry_run=True)

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ingest_paper(tmp_path / "no.yaml", dry_run=True)

    def test_empty_directory(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ingest_directory(tmp_path, dry_run=True)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCli:
    def test_dry_run_returns_zero(self):
        rc = _cli_main(["--dry-run"])
        assert rc == 0

    def test_missing_dir_returns_2(self):
        rc = _cli_main(["--from", "/no/such/dir", "--dry-run"])
        assert rc == 2
