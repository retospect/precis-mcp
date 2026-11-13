"""gr661902: a kindless id that resolves to nothing must say WHY.

Residual cluster of prod call-errors fingerprint
``callerr:364f881cade8793c`` after gr648740's inference landed —

    get(id='tex--dft_methodology')   # no such file on disk
    get(id=1518127)                  # no live ref by that id
    get(id=49234)                    # …

— all still answered with ``[error:BadInput] missing kind=`` plus a
generic "pick a kind" line that never mentioned the id the caller
already had, so sessions retried the identical call. The refusal
itself stands — a kind is still never guessed — but its ``next:``
now leads with the diagnosis: no such ref / a tombstone naming its
kind / a kind not live in this build / a file address that parses
for specific kinds but names nothing on disk / a stem that names
files under 2+ kinds (ambiguous ownership is not "no such file").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.runtime import PrecisRuntime
from precis.store import Store


@pytest.fixture
def runtime(store: Store) -> PrecisRuntime:
    from precis.embedder import make_embedder

    embedder = make_embedder("mock", dim=store.embedding_dim())
    return PrecisRuntime(
        config=PrecisConfig(),
        hub=boot(store=store, embedder=embedder),
    )


@pytest.fixture
def rooted_runtime(store: Store, tmp_path: Path) -> PrecisRuntime:
    """Runtime with the file kinds live (PRECIS_ROOT) and one .tex file."""
    ws = tmp_path / "ws"
    (ws / "tex").mkdir(parents=True)
    (ws / "tex" / "catalyst_landscape.tex").write_text(
        "\\section{Intro}\nCatalyst landscape body.\n", encoding="utf-8"
    )
    hub = boot(store=store, precis_root=str(ws))
    return PrecisRuntime(config=PrecisConfig(), hub=hub)


def _todo_id(runtime: PrecisRuntime, text: str) -> int:
    m = re.search(r"id=(\d+)", runtime.dispatch("put", {"kind": "todo", "text": text}))
    assert m is not None
    return int(m.group(1))


class TestBareNumericIdDiagnosis:
    def test_unknown_int_names_no_ref(self, runtime: PrecisRuntime) -> None:
        out = runtime.dispatch("get", {"id": 987654321})
        assert "[error:BadInput]" in out
        assert "missing kind" in out
        assert "names no ref" in out

    def test_unknown_digit_string_names_no_ref(self, runtime: PrecisRuntime) -> None:
        out = runtime.dispatch("get", {"id": "987654321"})
        assert "missing kind" in out
        assert "names no ref" in out

    def test_deleted_ref_names_its_kind(self, runtime: PrecisRuntime) -> None:
        """Still a refusal (never a kind guessed off a tombstone), but
        the hint now names the dead ref's kind so the retry under an
        explicit ``kind=`` reaches an informative Gone."""
        tid = _todo_id(runtime, "doomed todo")
        runtime.dispatch("delete", {"kind": "todo", "id": tid})
        out = runtime.dispatch("get", {"id": tid})
        assert "missing kind" in out
        assert "deleted todo ref" in out
        assert "kind='todo'" in out

    def test_non_inferring_verb_names_live_kind(self, runtime: PrecisRuntime) -> None:
        """``delete``/``edit`` deliberately skip bare-id inference —
        the refusal still names the resolved kind so the retry is
        mechanical, not a guess."""
        tid = _todo_id(runtime, "still live")
        out = runtime.dispatch("delete", {"id": tid})
        assert "missing kind" in out
        assert "live todo ref" in out
        assert "kind='todo'" in out

    def test_ref_of_disabled_kind_is_named(
        self, runtime: PrecisRuntime, store: Store
    ) -> None:
        """A bare id naming a ref whose kind this build doesn't serve
        says so outright instead of looking like a typo."""
        ref = store.insert_ref(
            kind="tex", slug="ghost-file", title="G", provider="manual"
        )
        assert "tex" not in runtime.hub.kinds  # no PRECIS_ROOT in this boot
        out = runtime.dispatch("get", {"id": ref.id})
        assert "missing kind" in out
        assert "tex" in out
        assert "not live in this build" in out

    def test_link_unknown_id_keeps_call_shape_hint(
        self, runtime: PrecisRuntime
    ) -> None:
        dst = _todo_id(runtime, "link dst")
        out = runtime.dispatch("link", {"id": 987654321, "target": f"todo:{dst}"})
        assert "missing kind" in out
        assert "names no ref" in out
        assert "link(kind=" in out  # gr518636 shape hint still present


class TestFileShapeDiagnosis:
    def test_missing_file_id_names_file_kinds(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        out = rooted_runtime.dispatch("get", {"id": "tex--dft_methodology"})
        assert "[error:BadInput]" in out
        assert "missing kind" in out
        assert "file address" in out
        assert "tex" in out

    def test_explicit_ext_names_only_that_kind(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        out = rooted_runtime.dispatch("get", {"id": "notes/meeting.tex"})
        assert "missing kind" in out
        assert "parses as a tex file address" in out

    def test_ambiguous_stem_names_owners_not_missing(
        self, store: Store, tmp_path: Path
    ) -> None:
        """``meeting.md`` + ``meeting.tex`` both exist: the stem is
        ambiguous, never guessed — and the diagnosis must say that,
        not the false "no such file exists on disk" the claimants-only
        hint produced (review on gr661902's fix)."""
        ws = tmp_path / "ws"
        (ws / "notes").mkdir(parents=True)
        (ws / "notes" / "meeting.md").write_text("# M\n", encoding="utf-8")
        (ws / "notes" / "meeting.tex").write_text("T\n", encoding="utf-8")
        hub = boot(store=store, precis_root=str(ws))
        rt = PrecisRuntime(config=PrecisConfig(), hub=hub)
        out = rt.dispatch("get", {"id": "notes--meeting"})
        assert "missing kind" in out
        assert "no such file" not in out
        assert "ambiguous" in out
        assert "markdown" in out
        assert "tex" in out

    def test_bare_slug_gets_no_file_diagnosis(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        """A single-token slug is equally a file slug and a paper/gripe
        slug — no file signal, so no file-shaped claim."""
        out = rooted_runtime.dispatch("get", {"id": "dft_methodology"})
        assert "missing kind" in out
        assert "file address" not in out
        assert "pass kind=" in out

    def test_existing_file_still_self_identifies(
        self, rooted_runtime: PrecisRuntime
    ) -> None:
        """The gr648740 inference path is untouched: an id that names a
        live file never reaches the refusal."""
        out = rooted_runtime.dispatch("get", {"id": "tex--catalyst_landscape"})
        assert "missing kind" not in out
        assert "Catalyst landscape body." in out
