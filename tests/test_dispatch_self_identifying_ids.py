"""gr648740: kind-less ``get`` on self-identifying ids must not bounce.

Prod call-errors fingerprint ``callerr:364f881cade8793c``: seven
``get(kind=None)`` rejections in 24h across six sessions —

    get(id='main::precis.utils.llm.decide._rejection', view='source')
    get(id='tex--catalyst_landscape')

— all answered with ``[error:BadInput] missing kind=``; one session
repeated the identical failing call after seeing the error. Both id
shapes name their kind unambiguously: ``<alias>::<qualname>`` is the
python kind's address grammar (the alias is a configured root), and a
prose-file slug/path names its kind via the extension of the file it
addresses. The dispatcher now infers the kind in both cases; unknown or
ambiguous ids keep the honest refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.runtime import PrecisRuntime
from precis.store import Store


@pytest.fixture
def rooted_runtime(store: Store, tmp_path: Path) -> PrecisRuntime:
    """Runtime with python root alias ``main`` and a PRECIS_ROOT tree
    holding one .tex file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 'source-of-f'\n", encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / "tex").mkdir(parents=True)
    (ws / "tex" / "catalyst_landscape.tex").write_text(
        "\\section{Intro}\nCatalyst landscape body.\n", encoding="utf-8"
    )
    hub = boot(store=store, precis_root=str(ws), python_roots=f"main:{repo}")
    return PrecisRuntime(config=PrecisConfig(), hub=hub)


class TestPythonQualnameId:
    def test_get_qualname_id_without_kind(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch("get", {"id": "main::m.f", "view": "source"})
        assert "missing kind" not in out
        assert "[error" not in out
        assert "source-of-f" in out

    def test_unknown_alias_still_refuses(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch("get", {"id": "bogus::m.f"})
        assert "[error:BadInput]" in out
        assert "missing kind" in out

    def test_python_kind_absent_still_refuses(self, store: Store) -> None:
        hub = boot(store=store)  # no python_roots configured
        rt = PrecisRuntime(config=PrecisConfig(), hub=hub)
        out = rt.dispatch("get", {"id": "main::m.f"})
        assert "[error:BadInput]" in out
        assert "missing kind" in out


class TestProseFileId:
    def test_get_tex_slug_without_kind(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch("get", {"id": "tex--catalyst_landscape"})
        assert "missing kind" not in out
        assert "[error" not in out
        assert "Catalyst landscape body." in out

    def test_get_tex_slug_with_selector_without_kind(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        out = rooted_runtime.dispatch("get", {"id": "tex--catalyst_landscape~0"})
        assert "missing kind" not in out
        assert "[error" not in out
        assert "\\section{Intro}" in out

    def test_missing_file_still_refuses(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch("get", {"id": "tex--no-such-file"})
        assert "[error:BadInput]" in out
        assert "missing kind" in out

    def test_ambiguous_stem_still_refuses(self, store: Store, tmp_path: Path) -> None:
        """Same stem under two extensions owns two kinds — never guessed."""
        ws = tmp_path / "ws"
        (ws / "notes").mkdir(parents=True)
        (ws / "notes" / "meeting.md").write_text("# M\n", encoding="utf-8")
        (ws / "notes" / "meeting.tex").write_text("T\n", encoding="utf-8")
        hub = boot(store=store, precis_root=str(ws))
        rt = PrecisRuntime(config=PrecisConfig(), hub=hub)
        out = rt.dispatch("get", {"id": "notes--meeting"})
        assert "[error:BadInput]" in out
        assert "missing kind" in out

    def test_path_form_id_infers_kind(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch("get", {"id": "tex/catalyst_landscape.tex"})
        assert "missing kind" not in out
        assert "[error" not in out
        assert "Catalyst landscape body." in out

    def test_explicit_kind_wins(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch(
            "get", {"kind": "tex", "id": "tex--catalyst_landscape"}
        )
        assert "[error" not in out
        assert "Catalyst landscape body." in out
