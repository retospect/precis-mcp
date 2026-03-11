"""Tests for tools.py — full tool integration tests.

RAKE precis generated fresh each call — no sidecar, no LLM.
"""

from pathlib import Path

import pytest

from precis.tools import (
    Session,
    PrecisError,
    _load_nodes,
    activate,
    get,
    move,
    put,
    toc,
)


@pytest.fixture
def session():
    return Session()


# ─── Activate ────────────────────────────────────────────────────────


class TestActivate:
    @pytest.mark.asyncio
    async def test_open_docx(self, session, tmp_docx):
        result = await activate(session, str(tmp_docx))
        assert "📄 test.docx" in result
        assert "6 nodes" in result
        assert session.active_file == str(tmp_docx)

    @pytest.mark.asyncio
    async def test_open_tex(self, session, tmp_tex):
        result = await activate(session, str(tmp_tex))
        assert "📄 main.tex" in result
        assert "methods.tex" in result
        assert session.active_file == str(tmp_tex)

    @pytest.mark.asyncio
    async def test_create_docx(self, session, tmp_path):
        new_path = str(tmp_path / "new.docx")
        result = await activate(session, new_path)
        assert "created, 0 nodes" in result
        assert Path(new_path).exists()

    @pytest.mark.asyncio
    async def test_create_tex(self, session, tmp_path):
        new_path = str(tmp_path / "new.tex")
        result = await activate(session, new_path)
        assert "created, 0 nodes" in result
        assert Path(new_path).exists()

    @pytest.mark.asyncio
    async def test_unsupported_format(self, session, tmp_path):
        with pytest.raises(PrecisError, match="Unsupported"):
            await activate(session, str(tmp_path / "file.pdf"))

    @pytest.mark.asyncio
    async def test_rake_precis_generated(self, session, tmp_docx):
        """Activate generates RAKE precis for paragraph nodes."""
        await activate(session, str(tmp_docx))
        nodes = _load_nodes(str(tmp_docx))
        paras = [n for n in nodes if n.node_type == "p"]
        for p in paras:
            assert p.precis, f"Node {p.slug} has no precis"
            assert ";" in p.precis  # RAKE phrases joined with ;


# ─── Toc ─────────────────────────────────────────────────────────────


class TestToc:
    @pytest.mark.asyncio
    async def test_full_toc(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await toc(session)
        assert "📄 test.docx" in result
        assert "Introduction" in result
        assert "Methods" in result

    @pytest.mark.asyncio
    async def test_toc_pipe_separator(self, session, tmp_docx):
        """Every toc line has a | separator."""
        await activate(session, str(tmp_docx))
        result = await toc(session)
        for line in result.strip().split("\n"):
            if line and not line.startswith("📄") and line.strip():
                assert "|" in line, f"Missing | in: {line}"

    @pytest.mark.asyncio
    async def test_toc_heading_hash(self, session, tmp_docx):
        """Heading lines have #| prefix."""
        await activate(session, str(tmp_docx))
        result = await toc(session)
        lines = result.strip().split("\n")
        heading_lines = [l for l in lines if "#|" in l]
        assert len(heading_lines) >= 2  # Introduction + Methods

    @pytest.mark.asyncio
    async def test_scope(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await toc(session, scope="H1.1")
        assert "Methods" in result or "H1.1" in result

    @pytest.mark.asyncio
    async def test_grep(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await toc(session, grep="wibble")
        assert "grep: wibble" in result
        assert "hit" in result

    @pytest.mark.asyncio
    async def test_grep_no_match(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await toc(session, grep="zzzznonexistent")
        assert "0 hits" in result

    @pytest.mark.asyncio
    async def test_no_active_file(self, session):
        with pytest.raises(PrecisError, match="no active file"):
            await toc(session)

    @pytest.mark.asyncio
    async def test_latex_toc_has_source_file(self, session, tmp_tex):
        """LaTeX toc lines show source file:start-end."""
        await activate(session, str(tmp_tex))
        result = await toc(session)
        # At least one line should have a .tex filename
        assert any(".tex:" in line for line in result.split("\n"))

    # ─── Depth filtering ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_depth_0_shows_everything(self, session, tmp_docx):
        """depth=0 (default) returns headings AND content nodes."""
        await activate(session, str(tmp_docx))
        result = await toc(session, depth=0)
        # Should have both heading lines (#|) and content lines (|)
        lines = result.strip().split("\n")
        heading_lines = [l for l in lines if "#|" in l]
        content_lines = [l for l in lines if "|" in l and "#|" not in l
                         and not l.startswith("📄") and "PATH" not in l]
        assert len(heading_lines) >= 2
        assert len(content_lines) >= 1

    @pytest.mark.asyncio
    async def test_depth_1_h1_only(self, session, tmp_docx):
        """depth=1 shows only H1 headings."""
        await activate(session, str(tmp_docx))
        result = await toc(session, depth=1)
        lines = [l for l in result.strip().split("\n")
                 if l.strip() and not l.startswith("📄") and "PATH" not in l]
        # Should have Introduction (H1) but NOT Methods (H2)
        assert any("#| Introduction" in l for l in lines)
        assert not any("##|" in l for l in lines)
        # No content nodes
        content_lines = [l for l in lines if "|" in l and "#|" not in l]
        assert len(content_lines) == 0

    @pytest.mark.asyncio
    async def test_depth_2_h1_and_h2(self, session, tmp_docx):
        """depth=2 shows H1 and H2 headings, no content."""
        await activate(session, str(tmp_docx))
        result = await toc(session, depth=2)
        lines = [l for l in result.strip().split("\n")
                 if l.strip() and not l.startswith("📄") and "PATH" not in l]
        assert any("#| Introduction" in l for l in lines)
        assert any("##| Methods" in l for l in lines)
        # No content nodes
        content_lines = [l for l in lines if "|" in l and "#|" not in l]
        assert len(content_lines) == 0

    @pytest.mark.asyncio
    async def test_depth_4_all_headings_no_content(self, session, tmp_docx):
        """depth=4 shows all heading levels but no content nodes."""
        await activate(session, str(tmp_docx))
        result = await toc(session, depth=4)
        lines = [l for l in result.strip().split("\n")
                 if l.strip() and not l.startswith("📄") and "PATH" not in l]
        # Only heading lines
        for line in lines:
            assert "#|" in line, f"Non-heading line at depth=4: {line}"

    @pytest.mark.asyncio
    async def test_depth_with_scope(self, session, tmp_docx):
        """depth + scope compose: filter section then by heading level."""
        await activate(session, str(tmp_docx))
        # Scope to section with Methods (H2), but depth=1 should exclude H2
        full = await toc(session, scope="H1")
        assert "Methods" in full  # H2 under H1 is visible by default
        filtered = await toc(session, scope="H1", depth=1)
        assert "Methods" not in filtered  # depth=1 excludes H2

    # ─── Scope shorthand ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_scope_shorthand_h1(self, session, tmp_docx):
        """scope='H1' matches H1.x.x.x paths (shorthand works via startswith)."""
        await activate(session, str(tmp_docx))
        result = await toc(session, scope="H1")
        lines = [l for l in result.strip().split("\n")
                 if l.strip() and not l.startswith("📄") and "PATH" not in l]
        # All data lines should be in section H1.*
        for line in lines:
            assert "H1." in line, f"Non-H1 line in scope='H1': {line}"
        assert len(lines) > 0

    @pytest.mark.asyncio
    async def test_scope_shorthand_h1_dot_1(self, session, tmp_docx):
        """scope='H1.1' narrows to subsection 1.1."""
        await activate(session, str(tmp_docx))
        result = await toc(session, scope="H1.1")
        lines = [l for l in result.strip().split("\n")
                 if l.strip() and not l.startswith("📄") and "PATH" not in l]
        for line in lines:
            assert "H1.1." in line, f"Non-H1.1 line: {line}"

    # ─── Auto-adaptive large docs ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_large_doc_auto_truncates(self, session, large_docx):
        """Large documents auto-truncate to headings-only with hint."""
        await activate(session, str(large_docx))
        result = await toc(session)
        assert "⚠ Large document" in result
        assert "showing headings only" in result
        # Should not have content lines (auto depth=4)
        lines = [l for l in result.strip().split("\n")
                 if "|" in l and "#|" not in l
                 and not l.startswith("📄") and "PATH" not in l
                 and "⚠" not in l and "Drill" not in l]
        assert len(lines) == 0

    @pytest.mark.asyncio
    async def test_large_doc_scoped_shows_all(self, session, large_docx):
        """Scoped toc on large doc shows full detail (no auto-truncation)."""
        await activate(session, str(large_docx))
        result = await toc(session, scope="H1.1")
        # Should NOT auto-truncate when scope is set
        assert "⚠ Large document" not in result
        # Should have content lines
        lines = [l for l in result.strip().split("\n")
                 if "|" in l and "#|" not in l
                 and not l.startswith("📄") and "PATH" not in l]
        assert len(lines) > 0

    @pytest.mark.asyncio
    async def test_large_doc_explicit_depth_0(self, session, large_docx):
        """Explicit depth=0 on large doc — auto-truncate still fires (depth=0 is default)."""
        await activate(session, str(large_docx))
        result = await toc(session, depth=0)
        assert "⚠ Large document" in result

    @pytest.mark.asyncio
    async def test_large_doc_explicit_depth_1(self, session, large_docx):
        """Explicit depth=1 on large doc — no auto hint, just depth filter."""
        await activate(session, str(large_docx))
        result = await toc(session, depth=1)
        assert "⚠ Large document" not in result
        # Only H1 lines
        lines = [l for l in result.strip().split("\n")
                 if l.strip() and "#|" in l]
        for line in lines:
            assert "##|" not in line  # no H2+

    @pytest.mark.asyncio
    async def test_header_shows_node_counts(self, session, tmp_docx):
        """Header shows filtered/total counts when they differ."""
        await activate(session, str(tmp_docx))
        result = await toc(session, depth=1)
        assert "/ " in result  # "N nodes / M total"


    # ─── LaTeX depth / scope / label hint ──────────────────────────

    @pytest.mark.asyncio
    async def test_latex_activate_label_hint(self, session, tmp_tex):
        """LaTeX activate shows label hint with example."""
        result = await activate(session, str(tmp_tex))
        assert "\\label{}" in result
        assert "sec:methods" in result

    @pytest.mark.asyncio
    async def test_latex_depth_1(self, session, tmp_tex):
        """depth=1 on LaTeX shows only \\section headings."""
        await activate(session, str(tmp_tex))
        result = await toc(session, depth=1)
        lines = [l for l in result.strip().split("\n")
                 if l.strip() and "#|" in l]
        assert len(lines) >= 2  # Introduction + Methods
        # No content nodes
        content_lines = [l for l in result.strip().split("\n")
                         if "|" in l and "#|" not in l
                         and not l.startswith("📄") and "PATH" not in l]
        assert len(content_lines) == 0

    @pytest.mark.asyncio
    async def test_latex_depth_0_shows_content(self, session, tmp_tex):
        """depth=0 on LaTeX shows headings + content with source locations."""
        await activate(session, str(tmp_tex))
        result = await toc(session, depth=0)
        assert ".tex:" in result  # source file locations visible

    @pytest.mark.asyncio
    async def test_latex_scope_shorthand(self, session, tmp_tex):
        """scope='H2' narrows to Methods section in LaTeX."""
        await activate(session, str(tmp_tex))
        result = await toc(session, scope="H2")
        assert "Methods" in result
        assert "Introduction" not in result

    @pytest.mark.asyncio
    async def test_latex_scope_with_depth(self, session, tmp_tex):
        """scope + depth compose for LaTeX."""
        await activate(session, str(tmp_tex))
        # Full detail in Methods section
        full = await toc(session, scope="H2")
        # Headings only in Methods
        headings_only = await toc(session, scope="H2", depth=1)
        # Full should have more lines
        full_lines = [l for l in full.strip().split("\n") if l.strip()]
        head_lines = [l for l in headings_only.strip().split("\n") if l.strip()]
        assert len(full_lines) > len(head_lines)

    @pytest.mark.asyncio
    async def test_docx_no_label_hint(self, session, tmp_docx):
        """DOCX activate does NOT show label hint."""
        result = await activate(session, str(tmp_docx))
        assert "\\label" not in result


# ─── Get ─────────────────────────────────────────────────────────────


class TestGet:
    @pytest.mark.asyncio
    async def test_by_slug(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        nodes = _load_nodes(str(tmp_docx))
        para = [n for n in nodes if n.node_type == "p"][0]

        result = await get(session, id=para.slug)
        assert ">>" in result
        assert "wibble" in result.lower()

    @pytest.mark.asyncio
    async def test_by_path(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await get(session, id="H1.0.0.0p1")
        assert ">>" in result

    @pytest.mark.asyncio
    async def test_heading_returns_children(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await get(session, id="H1.0.0.0")
        assert "Introduction" in result
        assert ">>" in result

    @pytest.mark.asyncio
    async def test_stale_slug_error(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        with pytest.raises(PrecisError, match="not found"):
            await get(session, id="ZZZZZ")

    @pytest.mark.asyncio
    async def test_comma_separated(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        nodes = _load_nodes(str(tmp_docx))
        paras = [n for n in nodes if n.node_type == "p"][:2]
        ids = f"{paras[0].slug},{paras[1].slug}"

        result = await get(session, id=ids)
        assert result.count(">>") >= 2

    @pytest.mark.asyncio
    async def test_latex_label(self, session, tmp_tex):
        await activate(session, str(tmp_tex))
        result = await get(session, id="sec:methods")
        assert "Methods" in result

    @pytest.mark.asyncio
    async def test_no_active_file(self, session):
        with pytest.raises(PrecisError, match="no active file"):
            await get(session, id="ABC12")


# ─── Put ─────────────────────────────────────────────────────────────


class TestPut:
    @pytest.mark.asyncio
    async def test_replace(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        nodes = _load_nodes(str(tmp_docx))
        para = [n for n in nodes if n.node_type == "p"][0]

        result = await put(
            session,
            id=para.slug,
            text="Completely new text.",
            mode="replace",
            tracked=False,
        )
        assert "→" in result
        assert "replace" in result

    @pytest.mark.asyncio
    async def test_insert_after(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        nodes = _load_nodes(str(tmp_docx))
        para = [n for n in nodes if n.node_type == "p"][0]

        result = await put(
            session, id=para.slug, text="New paragraph.", mode="after", tracked=False
        )
        assert "+" in result
        assert "after" in result

    @pytest.mark.asyncio
    async def test_delete(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        nodes = _load_nodes(str(tmp_docx))
        para = [n for n in nodes if n.node_type == "p"][0]

        result = await put(session, id=para.slug, mode="delete")
        assert "-" in result
        assert "deleted" in result

    @pytest.mark.asyncio
    async def test_append(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await put(session, text="Appended text.", mode="append")
        assert "+" in result

    @pytest.mark.asyncio
    async def test_append_heading(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await put(session, text="# Conclusion", mode="append")
        assert "+" in result

    @pytest.mark.asyncio
    async def test_multi_paragraph_auto_split(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await put(session, text="Para one.\n\nPara two.", mode="append")
        assert "Auto-split: 2 paragraphs" in result

    @pytest.mark.asyncio
    async def test_citation_hints_on_undefined(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        result = await put(
            session,
            text="MOFs show high uptake [@sumida2012] and selectivity [@jones2020].",
            mode="append",
        )
        assert "undefined citation" in result
        assert "[@sumida2012]" in result
        assert "[@jones2020]" in result
        assert "mode='append'" in result  # hint shows how to define

    @pytest.mark.asyncio
    async def test_citation_hints_none_when_defined(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        await put(session, text="Great uptake [@foo2020].", mode="append")
        result = await put(
            session,
            text="[@foo2020]: Foo et al., Title, J. Chem., 2020.",
            mode="append",
        )
        assert "Undefined citations" not in result

    @pytest.mark.asyncio
    async def test_invalid_mode(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        with pytest.raises(PrecisError, match="invalid mode"):
            await put(session, text="foo", mode="invalid")

    @pytest.mark.asyncio
    async def test_no_id_for_replace(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        with pytest.raises(PrecisError, match="id required"):
            await put(session, text="foo", mode="replace")

    @pytest.mark.asyncio
    async def test_no_active_file(self, session):
        with pytest.raises(PrecisError, match="no active file"):
            await put(session, text="foo", mode="append")

    @pytest.mark.asyncio
    async def test_latex_replace(self, session, tmp_tex):
        await activate(session, str(tmp_tex))
        nodes = _load_nodes(str(tmp_tex))
        para = [n for n in nodes if n.node_type == "p"][0]

        result = await put(
            session, id=para.slug, text="New LaTeX text.", mode="replace"
        )
        assert "→" in result


# ─── Move ────────────────────────────────────────────────────────────


class TestMove:
    @pytest.mark.asyncio
    async def test_move_docx(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        nodes = _load_nodes(str(tmp_docx))
        paras = [n for n in nodes if n.node_type == "p"]
        assert len(paras) >= 2

        result = await move(session, id=paras[0].slug, after=paras[1].slug)
        assert "moved" in result
        assert "→" in result

    @pytest.mark.asyncio
    async def test_move_not_found(self, session, tmp_docx):
        await activate(session, str(tmp_docx))
        with pytest.raises(PrecisError, match="not found"):
            await move(session, id="ZZZZZ", after="YYYYY")

    @pytest.mark.asyncio
    async def test_no_active_file(self, session):
        with pytest.raises(PrecisError, match="no active file"):
            await move(session, id="A", after="B")


# ─── Session ─────────────────────────────────────────────────────────


class TestSession:
    def test_require_active_raises(self):
        session = Session()
        with pytest.raises(PrecisError, match="no active file"):
            session.require_active()
