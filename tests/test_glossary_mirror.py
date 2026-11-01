"""Glossary mirror (gr548266): ``docs/glossary.md`` → ``repo-dev-glossary``
taxon + alias nodes via ``precis taxonomy import-glossary``.

Pins: the five-entry fixture mints five taxa (``SPACE:repo-dev``,
``meta.file_mirror`` path/line/namespace, gloss as definition, pointer,
aliases) and two ``same-as`` alias rows in scope ``repo-dev-glossary`` —
the ``(lit: …)`` one carrying ``meta.kind='literature'``. Re-import is a
no-op; ``--sync`` soft-deletes a removed term's node and alias rows;
namespace-scoped dedup means a same-named science taxon is untouched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from precis.taxonomy import glossary
from precis.taxonomy.glossary import (
    GLOSSARY_NAMESPACE,
    import_glossary,
    list_mirrored_terms,
    parse_glossary,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

FIXTURE = """\
# Fixture glossary

Intro prose — not an entry.

## Coined terms

- **dark** — shipped dark on this machine, no humans in the loop. → `scripts/ship`
- **fixer** — the laptop that fixes gripes end-to-end. → `src/precis/fixer/__init__.py`
- **minter** (legacy: `dispatch` worker) — the registry's nanopub minter. → `src/precis/taproot/nanopub.py`

## Overloaded — which one?

- **extent ladder** — the sbom degree-of-interest ladder (lit: degree-of-interest function). → `src/precis/eye/sbom.py`
- **mofs-for-electrodes** — → todo `td48056`
"""


def _write(tmp_path: Path, text: str = FIXTURE) -> Path:
    path = tmp_path / "glossary.md"
    path.write_text(text, encoding="utf-8")
    return path


def _taxa(store: Any, namespace: str = GLOSSARY_NAMESPACE) -> dict[str, Any]:
    """norm-less name → Ref for live mirrored taxa."""
    out = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='taxon' AND retired_at IS NULL "
            "AND meta->'file_mirror'->>'namespace' = %s",
            (namespace,),
        ).fetchall()
    for (rid,) in rows:
        ref = store.get_ref(kind="taxon", id=rid)
        out[str(ref.meta["name"])] = ref
    return out


def _alias_rows(store: Any) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind='alias' "
            "AND retired_at IS NULL AND meta->'alias'->>'scope' = %s "
            "ORDER BY ref_id",
            (GLOSSARY_NAMESPACE,),
        ).fetchall()
    return [{"id": rid, "meta": meta} for rid, meta in rows]


def _live_names(store: Any, name: str) -> int:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM refs WHERE kind='taxon' AND retired_at IS NULL "
            "AND meta->>'norm_name' = %s",
            (name,),
        ).fetchone()[0]


def _retired(store: Any, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT retired_at IS NOT NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()[0]


def _has_repo_dev_tag(store: Any, ref_id: int) -> bool:
    return any(
        t.namespace == "closed" and t.prefix == "SPACE" and t.value == "repo-dev"
        for t in store.tags_for(ref_id)
    )


class TestParse:
    def test_five_entries_fields_and_aliases(self) -> None:
        entries = parse_glossary(FIXTURE)
        assert [e.term for e in entries] == [
            "dark",
            "fixer",
            "minter",
            "extent ladder",
            "mofs-for-electrodes",
        ]
        dark = entries[0]
        assert dark.gloss == "shipped dark on this machine, no humans in the loop."
        assert dark.pointer == "`scripts/ship`"
        minter = entries[2]
        assert minter.aliases == ["dispatch"]
        assert minter.lit == frozenset()
        ladder = entries[3]
        assert ladder.aliases == ["degree-of-interest function"]
        assert ladder.lit == frozenset({"degree-of-interest function"})
        mofs = entries[4]
        assert mofs.gloss == ""
        assert mofs.pointer == "todo `td48056`"
        lines = {e.term: e.line for e in entries}
        assert lines["dark"] == 7 and lines["mofs-for-electrodes"] == 14

    def test_real_glossary_parses(self) -> None:
        text = (REPO_ROOT / "docs" / "glossary.md").read_text(encoding="utf-8")
        entries = parse_glossary(text)
        assert len(entries) >= 130
        by_term = {e.term: e for e in entries}
        assert "dark factory" in by_term
        assert by_term["dark factory"].pointer.startswith("`src/precis/fixer/")
        # `**a** / **b**` entries: the second bold span is an alias.
        assert "gold-sea-2" in by_term["gold-sea"].aliases
        # A `lit` marker yields literature-flagged aliases.
        assert "multi-fidelity methods" in by_term["fidelity levels"].lit
        # The backticked quest-id bullets are not terms.
        assert "161906" not in by_term


class TestImport:
    @pytest.fixture(autouse=True)
    def _alias_kind(self, store: Any) -> None:
        # 'alias' is a code-side KindSpec (registered at server boot),
        # not a baseline kinds row — the truncated test DB needs it.
        from precis.handlers.alias import AliasHandler

        store.upsert_kinds([AliasHandler.spec])

    def test_five_taxa_two_alias_rows(self, store: Any, tmp_path: Path) -> None:
        report = import_glossary(store, _write(tmp_path))
        assert (report.created, report.updated, report.unchanged) == (5, 0, 0)
        assert report.aliases_added == 2

        taxa = _taxa(store)
        assert sorted(taxa) == [
            "dark",
            "extent ladder",
            "fixer",
            "minter",
            "mofs-for-electrodes",
        ]
        dark = taxa["dark"]
        assert _has_repo_dev_tag(store, dark.id)
        assert dark.meta["definition"].startswith("shipped dark")
        assert dark.meta["pointer"] == "`scripts/ship`"
        assert dark.meta["file_mirror"]["namespace"] == GLOSSARY_NAMESPACE
        assert dark.meta["file_mirror"]["line"] == 7
        assert dark.meta["file_mirror"]["path"].endswith("glossary.md")
        assert taxa["minter"].meta["aliases"] == ["dispatch"]

        rows = _alias_rows(store)
        pairs = {(r["meta"]["alias"]["a"], r["meta"]["alias"]["b"]) for r in rows}
        assert pairs == {
            ("dispatch", "minter"),
            ("degree-of-interest function", "extent ladder"),
        }
        assert all(r["meta"]["alias"]["rel"] == "same-as" for r in rows)
        lit = {
            r["meta"]["alias"]["a"]
            for r in rows
            if r["meta"].get("kind") == "literature"
        }
        assert lit == {"degree-of-interest function"}

    def test_reimport_is_a_no_op(self, store: Any, tmp_path: Path) -> None:
        path = _write(tmp_path)
        import_glossary(store, path)
        report = import_glossary(store, path)
        assert (report.created, report.updated, report.retired) == (0, 0, 0)
        assert report.unchanged == 5
        assert report.aliases_added == 0 and report.aliases_retired == 0
        assert len(_taxa(store)) == 5 and len(_alias_rows(store)) == 2

    def test_dry_run_writes_nothing(self, store: Any, tmp_path: Path) -> None:
        report = import_glossary(store, _write(tmp_path), dry_run=True)
        assert report.created == 5 and report.aliases_added == 2
        assert _taxa(store) == {} and _alias_rows(store) == []

    def test_edited_entry_updates_in_place(self, store: Any, tmp_path: Path) -> None:
        path = _write(tmp_path)
        import_glossary(store, path)
        text = FIXTURE.replace("shipped dark", "merged dark").replace(
            "`scripts/ship`", "`scripts/ship --quick`"
        )
        path.write_text(text, encoding="utf-8")
        report = import_glossary(store, path)
        assert (report.created, report.updated, report.unchanged) == (0, 1, 4)
        dark = _taxa(store)["dark"]
        assert dark.meta["definition"].startswith("merged dark")
        assert dark.meta["pointer"] == "`scripts/ship --quick`"

    def test_removed_entry_soft_deletes_on_sync(
        self, store: Any, tmp_path: Path
    ) -> None:
        path = _write(tmp_path)
        import_glossary(store, path)
        ladder_id = _taxa(store)["extent ladder"].id
        drop = "\n".join(
            line for line in FIXTURE.splitlines() if "**extent ladder**" not in line
        )
        path.write_text(drop, encoding="utf-8")

        report = import_glossary(store, path, sync=True)
        assert report.retired == 1
        assert _retired(store, ladder_id)
        assert sorted(_taxa(store)) == [
            "dark",
            "fixer",
            "minter",
            "mofs-for-electrodes",
        ]
        # Its scope alias row retires with the term.
        assert {(r["meta"]["alias"]["a"]) for r in _alias_rows(store)} == {"dispatch"}

    def test_dropped_marker_retires_its_alias_row(
        self, store: Any, tmp_path: Path
    ) -> None:
        path = _write(tmp_path)
        import_glossary(store, path)
        path.write_text(
            FIXTURE.replace(" (legacy: `dispatch` worker)", ""), encoding="utf-8"
        )
        report = import_glossary(store, path)
        # minter's alias list changed (term still present → update, not
        # retire); its scope row retires, the lit row stays.
        assert (report.updated, report.retired) == (1, 0)
        assert report.aliases_retired == 1
        assert {(r["meta"]["alias"]["a"]) for r in _alias_rows(store)} == {
            "degree-of-interest function"
        }

    def test_removed_entry_survives_without_sync(
        self, store: Any, tmp_path: Path
    ) -> None:
        path = _write(tmp_path)
        import_glossary(store, path)
        drop = "\n".join(
            line
            for line in FIXTURE.splitlines()
            if "**mofs-for-electrodes**" not in line
        )
        path.write_text(drop, encoding="utf-8")
        report = import_glossary(store, path)
        assert report.retired == 0
        assert len(_taxa(store)) == 5

    def test_same_named_science_taxon_survives(
        self, store: Any, tmp_path: Path
    ) -> None:
        from precis.dispatch import Hub
        from precis.handlers.taxon import TaxonHandler

        h = TaxonHandler(hub=Hub(store=store))
        resp = h.put(text="dark — the physics sense, not the repo-dev one")
        m = re.search(r"\btn(\d+)\b", resp.body)
        assert m is not None, resp.body
        science_id = int(m.group(1))

        import_glossary(store, _write(tmp_path))
        assert _live_names(store, "dark") == 2
        ref = store.get_ref(kind="taxon", id=science_id)
        assert ref is not None and "file_mirror" not in (ref.meta or {})

        # And the reverse: a science put after the mirror exists still mints.
        resp2 = h.put(text="fixer — the chemistry sense")
        assert re.search(r"\btn(\d+)\b", resp2.body), resp2.body
        assert _live_names(store, "fixer") == 2

    def test_list_mirrored_terms(self, store: Any, tmp_path: Path) -> None:
        import_glossary(store, _write(tmp_path))
        assert list_mirrored_terms(store) == [
            "dark",
            "extent ladder",
            "fixer",
            "minter",
            "mofs-for-electrodes",
        ]

    def test_blob_sha_gates_second_pass(self, store: Any, tmp_path: Path) -> None:
        path = _write(tmp_path)
        sha = glossary.glossary_blob_sha(path)
        import_glossary(store, path, blob_sha=sha)
        report = import_glossary(store, path, blob_sha=sha)
        assert report.skipped and report.entries == 5
        # A different sha re-runs; --force ignores the stored one.
        forced = import_glossary(store, path, blob_sha=sha, force=True)
        assert not forced.skipped and forced.unchanged == 5

    def test_blob_sha_matches_git(self, tmp_path: Path) -> None:
        path = _write(tmp_path)
        git = subprocess.run(
            ["git", "hash-object", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if git.returncode == 0:
            assert glossary.glossary_blob_sha(path) == git.stdout.strip()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only bash script")
class TestMemoryLintCheck:
    """``scripts/memory-lint`` check 12 — coined term without a node."""

    def _run_lint(self, tmp_path: Path, terms: list[str]) -> str:
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "docs").mkdir()
        shutil.copy(REPO_ROOT / "scripts" / "memory-lint", repo / "scripts")
        (repo / "docs" / "glossary.md").write_text(FIXTURE, encoding="utf-8")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=False)
        # memory-lint exits early with no memory dir; check 12 sits after it.
        memdir = (
            tmp_path / ".claude" / "projects" / str(repo).replace("/", "-") / "memory"
        )
        memdir.mkdir(parents=True)
        (memdir / "MEMORY.md").write_text("# index\n", encoding="utf-8")
        terms_file = tmp_path / "terms.txt"
        terms_file.write_text("\n".join(terms) + "\n", encoding="utf-8")
        env = dict(
            os.environ,
            HOME=str(tmp_path),
            PRECIS_GLOSSARY_TERMS=str(terms_file),
            PRECIS_MEMORY_NODES=str(tmp_path / "no-nodes"),
            PRECIS_MEMORY_CACHE=str(tmp_path / "cache.md"),
        )
        out = subprocess.run(
            ["bash", "scripts/memory-lint"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    def test_missing_term_is_flagged(self, tmp_path: Path) -> None:
        out = self._run_lint(tmp_path, ["dark", "fixer", "minter", "extent ladder"])
        assert "coined term(s) without a repo-dev-glossary node" in out
        assert "mofs-for-electrodes" in out

    def test_complete_set_is_clean(self, tmp_path: Path) -> None:
        out = self._run_lint(
            tmp_path,
            ["dark", "extent ladder", "fixer", "minter", "mofs-for-electrodes"],
        )
        assert "every glossary term has a repo-dev-glossary node" in out
