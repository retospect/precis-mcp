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
        assert "Undefined citations" in result
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
