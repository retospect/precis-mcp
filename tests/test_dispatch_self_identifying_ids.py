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

    def test_explicit_extension_not_rerouted_to_other_kind(
        self, store: Store, tmp_path: Path
    ) -> None:
        """Only meeting.md exists: an explicit .tex path must not return it."""
        ws = tmp_path / "ws"
        (ws / "notes").mkdir(parents=True)
        (ws / "notes" / "meeting.md").write_text("# M\n\nmd body\n", encoding="utf-8")
        hub = boot(store=store, precis_root=str(ws))
        rt = PrecisRuntime(config=PrecisConfig(), hub=hub)
        out = rt.dispatch("get", {"id": "notes/meeting.tex"})
        assert "md body" not in out
        assert "missing kind" in out

    def test_explicit_extension_disambiguates_both_present(
        self, store: Store, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        (ws / "notes").mkdir(parents=True)
        (ws / "notes" / "meeting.md").write_text("# M\n\nmd body\n", encoding="utf-8")
        (ws / "notes" / "meeting.tex").write_text("tex body\n", encoding="utf-8")
        hub = boot(store=store, precis_root=str(ws))
        rt = PrecisRuntime(config=PrecisConfig(), hub=hub)
        tex = rt.dispatch("get", {"id": "notes/meeting.tex"})
        assert "tex body" in tex
        assert "missing kind" not in tex

    def test_explicit_kind_wins(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch(
            "get", {"kind": "tex", "id": "tex--catalyst_landscape"}
        )
        assert "[error" not in out
        assert "Catalyst landscape body." in out


class TestMissingKindIdWhy:
    """gr661902: the fingerprint recurred on ids that *look*
    self-identifying but resolve to nothing live — the bare
    ``missing kind=`` refusal never said why the id didn't route, so
    sessions repeated the identical call. The refusal now leads with
    the specific failure (dead numeric id, retired ref, file address
    no live file owns, unclaimed ``::`` alias) and, for a live ref of
    a kind this build doesn't serve, infers the kind anyway so the
    pointed kind error names it."""

    def test_dead_numeric_id_names_no_live_ref(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        out = rooted_runtime.dispatch("get", {"id": 987654321})
        assert "[error:BadInput]" in out
        assert "missing kind" in out
        assert "names no live ref" in out
        assert "pass kind=" in out
        assert "self-identifying" in out

    def test_retired_numeric_id_names_its_kind(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        import re

        put = rooted_runtime.dispatch("put", {"kind": "todo", "text": "soon gone"})
        tid = int(re.search(r"id=(\d+)", put).group(1))  # type: ignore[union-attr]
        rooted_runtime.dispatch("delete", {"kind": "todo", "id": tid})
        out = rooted_runtime.dispatch("get", {"id": tid})
        assert "missing kind" in out
        assert "retired todo" in out
        assert "kind='todo'" in out

    def test_live_ref_of_dead_kind_routes_to_kind_error(self, store: Store) -> None:
        """A bare numeric id naming a live ref of a kind this build
        doesn't serve infers the kind anyway — the "unknown kind" /
        "registered but disabled" error names it, beating
        ``missing kind=`` (e.g. prod ``get(id=1518127)`` on a gated
        kind)."""
        hub = boot(store=store)  # no precis_root → file kinds not live
        rt = PrecisRuntime(config=PrecisConfig(), hub=hub)
        assert "tex" not in hub.kinds
        ref = store.insert_ref(
            kind="tex", slug="ghost--file", title="Ghost", provider="manual"
        )
        out = rt.dispatch("get", {"id": ref.id})
        assert "missing kind" not in out
        assert "[error" in out
        assert "tex" in out

    def test_missing_file_slug_names_file_kinds(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        out = rooted_runtime.dispatch("get", {"id": "tex--no-such-file"})
        assert "missing kind" in out
        assert "no live file owns" in out
        assert "'tex'" in out

    def test_ambiguous_file_stem_names_owner_kinds(
        self, store: Store, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        (ws / "notes").mkdir(parents=True)
        (ws / "notes" / "meeting.md").write_text("# M\n", encoding="utf-8")
        (ws / "notes" / "meeting.tex").write_text("T\n", encoding="utf-8")
        hub = boot(store=store, precis_root=str(ws))
        rt = PrecisRuntime(config=PrecisConfig(), hub=hub)
        out = rt.dispatch("get", {"id": "notes--meeting"})
        assert "missing kind" in out
        assert "ambiguous" in out
        assert "'markdown'" in out
        assert "'tex'" in out

    def test_unknown_qualname_alias_points_at_python(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        out = rooted_runtime.dispatch("get", {"id": "bogus::m.f"})
        assert "missing kind" in out
        assert "kind='python'" in out

    def test_non_kind_colon_prefix_named(self, rooted_runtime: PrecisRuntime) -> None:
        out = rooted_runtime.dispatch("get", {"id": "nosuchkind:thing"})
        assert "missing kind" in out
        assert "'nosuchkind'" in out
        assert "not a live kind" in out
