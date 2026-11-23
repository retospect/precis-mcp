"""gr676949: kindless ``get`` self-identifies for a kind word, a skill
slug, and a bare DOI; a disabled ``kind:`` prefix names the way out.

2026-11-10..14 transcripts: ``get(id='skill', q=…)`` (4 calls), a bare
skill slug, ``tex:<slug>`` on a host where tex is disabled, and a bare
DOI each ended in ``[error:BadInput] missing kind=`` (or an unhelpful
variant), and ``precis-overview``/``precis-get-help`` contradicted each
other on whether ``kind=`` was required. The dispatcher now infers
kind='skill' from a bare kind word, kind='skill' from a served skill
slug, and kind='paper' from a bare DOI (full DOI kept — never split on
``/``); a ``kind:`` prefix naming a registered-but-disabled kind raises
``Unsupported`` naming the disabled-kinds skill and the node role that
reads it, instead of bouncing on the bare "missing kind=". Precedence:
real ref slugs are checked first — a slug is an address, a kind word is
not. ``put``/``edit`` stay kind-required (get-only inference).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.runtime import PrecisRuntime
from precis.store import Store

DOI = "10.1021/ja01019a011"
SKILLS = Path(__file__).resolve().parents[1] / "src/precis/data/skills"


@pytest.fixture
def runtime(store: Store) -> PrecisRuntime:
    from precis.embedder import make_embedder

    embedder = make_embedder("mock", dim=store.embedding_dim())
    return PrecisRuntime(
        config=PrecisConfig(),
        hub=boot(store=store, embedder=embedder),
    )


class TestKindNameInId:
    def test_kindless_get_kind_name_in_id(self, runtime: PrecisRuntime) -> None:
        """``get(id='skill', q='gripe')`` dispatches to the skill
        handler — the id moves to nothing, q= stays."""
        out = runtime.dispatch("get", {"id": "skill", "q": "gripe"})
        assert "[error" not in out
        assert "missing kind" not in out
        assert "gripe" in out

    def test_kind_word_lists_kind_default_page(self, runtime: PrecisRuntime) -> None:
        out = runtime.dispatch("get", {"id": "skill"})
        assert "[error" not in out
        assert "missing kind" not in out

    def test_kind_word_yields_to_real_ref_slug(
        self, runtime: PrecisRuntime, store: Store
    ) -> None:
        """A bare string equal to a kind name that is ALSO a live ref
        slug is ambiguous; slug lookup wins (refs checked first)."""
        ref = store.insert_ref(
            kind="paper",
            slug="skill",
            title="The word skill as a paper slug",
            provider="manual",
        )
        assert ref.kind == "paper"
        out = runtime.dispatch("get", {"id": "skill"})
        assert "[error" not in out
        assert "The word skill as a paper slug" in out


class TestSkillSlugAndDoi:
    def test_kindless_get_skill_slug(self, runtime: PrecisRuntime) -> None:
        out = runtime.dispatch("get", {"id": "precis-get-help"})
        assert "[error" not in out
        assert "missing kind" not in out
        # The skill handler answered: its frontmatter tags render on the
        # section index a whole-skill read serves.
        assert "orientation, verbs, addressing" in out

    def test_kindless_get_doi(self, runtime: PrecisRuntime) -> None:
        """A bare DOI routes to the read-only paper lookup."""
        out = runtime.dispatch("get", {"id": DOI})
        assert "[error:NotFound]" in out
        assert "not ingested" in out
        assert f"put(kind='paper', doi='{DOI}')" in out

    def test_unknown_string_still_reaches_missing_kind(
        self, runtime: PrecisRuntime
    ) -> None:
        out = runtime.dispatch("get", {"id": "zzz-not-a-slug-qqq"})
        assert "[error:BadInput]" in out
        assert "missing kind" in out

    def test_non_doi_numeric_shape_still_reaches_missing_kind(
        self, runtime: PrecisRuntime
    ) -> None:
        """``10.1/x`` fails the DOI grammar (``\\d{4,9}`` registrant)
        and stays an ordinary unknown string."""
        out = runtime.dispatch("get", {"id": "10.1/x"})
        assert "[error:BadInput]" in out
        assert "missing kind" in out


class TestDisabledKindPrefix:
    def test_disabled_kind_prefix_names_disabled_help(
        self, runtime: PrecisRuntime
    ) -> None:
        """``get(id='tex:<slug>')`` on a host without PRECIS_ROOT names
        the disabled kind and the disabled-kinds skill — not the bare
        "missing kind="."""
        assert "tex" not in runtime.hub.kinds  # no PRECIS_ROOT in this boot
        out = runtime.dispatch("get", {"id": "tex:chapters/intro"})
        assert "[error:Unsupported]" in out
        assert "kind 'tex' is disabled on this host" in out
        assert "precis-kinds-disabled-help" in out

    def test_live_kind_prefix_untouched(self, runtime: PrecisRuntime) -> None:
        """A ``kind:`` prefix for a live kind still splits normally."""
        tid = runtime.dispatch("put", {"kind": "todo", "text": "prefix probe"})
        m = re.search(r"id=(\d+)", tid)
        assert m is not None
        out = runtime.dispatch("get", {"id": f"todo:{m.group(1)}"})
        assert "[error" not in out
        assert "prefix probe" in out


def _next_calls(out: str, verb: str) -> list[str]:
    """The ``next:`` lines of a rendered refusal that are a ``verb(`` call."""
    return [
        line.removeprefix("  next: ").strip()
        for line in out.splitlines()
        if line.startswith(f"  next: {verb}(")
    ]


def _call_args(call: str) -> dict[str, Any]:
    """Keyword args of a printed ``verb(k=v, …)`` call, as dispatch takes them."""
    node = ast.parse(call, mode="eval").body
    assert isinstance(node, ast.Call)
    return {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}


class TestDisabledFileKindWorkerNext:
    """gr649944: a worker whose task names ``tex--<slug>`` files, on a host
    without PRECIS_ROOT, got "registered but disabled" plus a pointer to a
    skill that called the missing env "the operator's to fix" — 46 such
    errors in ~20 worker buckets on 2026-11-12, two or three retries per
    tick. The refusal now says not to retry and names the two things a
    worker can do: read the section as a memory, or park the todo with a
    reason."""

    @pytest.mark.parametrize(
        ("verb", "args"),
        [
            ("get", {"kind": "tex", "id": "tex--x"}),
            ("search", {"kind": "tex", "q": "design loop"}),
            ("get", {"id": "tex:chapters/intro"}),
        ],
    )
    def test_disabled_file_kind_names_both_worker_actions(
        self, runtime: PrecisRuntime, verb: str, args: dict[str, str]
    ) -> None:
        assert "tex" not in runtime.hub.kinds  # no PRECIS_ROOT in this boot
        out = runtime.dispatch(verb, args)
        assert "[error:Unsupported]" in out
        assert "missing PRECIS_ROOT" in out
        assert "do not retry" in out
        assert _next_calls(out, "search") == [
            "search(kind='memory', tags=['project:<project>'])"
        ]
        assert _next_calls(out, "tag") == [
            "tag(kind='todo', id=<todo id>, add=['STATUS:blocked'], "
            "reason='tex disabled on this host')"
        ]
        assert "precis-kinds-disabled-help" in out

    def test_worker_next_calls_run_as_printed(self, runtime: PrecisRuntime) -> None:
        """The two calls are accepted by the runtime exactly as the refusal
        prints them, once the placeholders are filled."""
        out = runtime.dispatch("get", {"kind": "tex", "id": "tex--x"})
        (search_call,) = _next_calls(out, "search")
        (tag_call,) = _next_calls(out, "tag")
        made = runtime.dispatch("put", {"kind": "todo", "text": "tex section fix"})
        m = re.search(r"id=(\d+)", made)
        assert m is not None

        found = runtime.dispatch(
            "search", _call_args(search_call.replace("<project>", "nox"))
        )
        assert "[error" not in found
        parked = runtime.dispatch(
            "tag", _call_args(tag_call.replace("<todo id>", m.group(1)))
        )
        assert "[error" not in parked
        assert "STATUS:blocked" in parked

    def test_skill_worker_section_names_the_same_two_calls(
        self, runtime: PrecisRuntime
    ) -> None:
        """``precis-kinds-disabled-help`` is where the refusal points; its
        worker section carries the same two calls (double-quoted, as skill
        fences write them)."""
        out = runtime.dispatch("get", {"kind": "tex", "id": "tex--x"})
        skill = (SKILLS / "precis-kinds-disabled-help.md").read_text(encoding="utf-8")
        assert "## I am a worker and the kind my task needs is off" in skill
        for call in _next_calls(out, "search") + _next_calls(out, "tag"):
            assert call.replace("'", '"') in skill

    def test_kindless_file_id_says_do_not_retry(self, runtime: PrecisRuntime) -> None:
        """``get(id='tex--x')`` with no ``kind=`` bounces on "missing kind=";
        its "pass kind=" hint invites the retry that cannot work, so the
        file-address line says so."""
        out = runtime.dispatch("get", {"id": "tex--x"})
        assert "missing kind" in out
        assert "not live in this build" in out
        assert "do not retry" in out
        assert "precis-kinds-disabled-help" in out

    def test_disabled_non_file_kind_keeps_plain_pointer(
        self, runtime: PrecisRuntime
    ) -> None:
        """Only the file kinds get the workspace wording: another disabled
        kind keeps the skill pointer and names no memory or todo call."""
        off = sorted(
            kind
            for kind, verdict in runtime.hub.loadabilities.items()
            if not verdict.loaded and kind not in {"markdown", "plaintext", "tex"}
        )
        assert off, "this boot gates out no non-file kind"
        out = runtime.dispatch("get", {"kind": off[0], "id": "x"})
        assert "[error:Unsupported]" in out
        assert "precis-kinds-disabled-help" in out
        assert "workspace files" not in out
        assert not _next_calls(out, "tag")

    def test_instruction_reason_keeps_plain_pointer(self) -> None:
        """A file kind switched off with an inline instruction (a draft tick
        gating ``tex``) already says what to do: no park-the-todo line."""
        from precis.runtime.dispatch import _disabled_kind_next

        hints = _disabled_kind_next(
            "tex", "write prose with put(kind='draft')", all_files_off=True
        )
        assert len(hints) == 1
        assert "precis-kinds-disabled-help" in hints[0]

    def test_kind_served_by_another_host_gets_no_worker_lines(
        self, runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When another node serves the kind, "read it there" is the way
        on: the refusal names the serving node's role (never its
        hostname, gr681314) and does not also say "do not retry" or
        park the todo."""
        monkeypatch.setattr(
            runtime.hub.store,
            "find_kind_provider_routes",
            lambda slug, **_: [("maple", "precis-worker-research")],
        )
        out = runtime.dispatch("get", {"id": "tex:chapters/intro"})
        assert "[error:Unsupported]" in out
        assert "kind 'tex' is disabled on this host" in out
        assert "routes through the research node" in out
        assert "maple" not in out
        assert "precis-kinds-disabled-help" in out
        assert "do not retry" not in out
        assert not _next_calls(out, "search")
        assert not _next_calls(out, "tag")

    def test_kind_served_by_several_hosts_of_one_role_gets_suffixes(
        self, runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two hosts running the same process share the role; the labels
        carry stable numeric suffixes and still leak no hostname."""
        monkeypatch.setattr(
            runtime.hub.store,
            "find_kind_provider_routes",
            lambda slug, **_: [
                ("maple", "precis-worker-research"),
                ("cedar", "precis-worker-research"),
            ],
        )
        out = runtime.dispatch("get", {"id": "tex:chapters/intro"})
        assert "routes through the research-1 node, research-2 node" in out
        assert "maple" not in out
        assert "cedar" not in out

    def test_unknown_kind_route_hint_names_role_not_hostname(
        self, runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr681314: the federation hint on an unknown kind names the
        serving node's role; the hostname never reaches the answer."""
        monkeypatch.setattr(
            runtime.hub.store,
            "find_kind_provider_routes",
            lambda slug, **_: [("maple", "precis-worker-fetch")],
        )
        out = runtime.dispatch("get", {"kind": "nosuchkind", "id": "x"})
        assert "unknown kind: nosuchkind" in out
        assert "routes through the fetch node" in out
        assert "maple" not in out

    def test_unknown_kind_without_routes_has_plain_hint(
        self, runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No live provider → no route hint at all, just the verb filter."""
        monkeypatch.setattr(
            runtime.hub.store, "find_kind_provider_routes", lambda slug, **_: []
        )
        out = runtime.dispatch("get", {"kind": "nosuchkind", "id": "x"})
        assert "unknown kind: nosuchkind" in out
        assert "routes through" not in out

    def test_one_file_kind_off_does_not_claim_files_unreadable(
        self, runtime: PrecisRuntime, store: Store, tmp_path: Path
    ) -> None:
        """Only with every file kind off does the refusal say the host
        cannot read workspace files. ``tex`` prohibited beside a live
        ``markdown`` names the one kind as off, on both the ``kind=``
        refusal and the kindless file-address hint."""
        all_off = runtime.dispatch("get", {"kind": "tex", "id": "tex--x"})
        assert "this host cannot read workspace files; do not retry" in all_off
        assert "this host cannot read workspace files" in runtime.dispatch(
            "get", {"id": "tex--x"}
        )

        partial = PrecisRuntime(
            config=PrecisConfig(),
            hub=boot(
                store=store,
                precis_root=str(tmp_path),
                kinds_disabled=frozenset({"tex"}),
            ),
        )
        assert "markdown" in partial.hub.kinds
        assert "tex" not in partial.hub.kinds
        out = partial.dispatch("get", {"kind": "tex", "id": "tex--x"})
        assert "[error:Unsupported]" in out
        assert "kind 'tex' is registered but disabled in this build" in out
        assert "kind 'tex' is off on this host; do not retry" in out
        assert "workspace files" not in out
        assert _next_calls(out, "search")
        assert _next_calls(out, "tag")

        kindless = partial.dispatch("get", {"id": "chapters/intro.tex"})
        assert "missing kind" in kindless
        assert "file kinds (tex) are not live in this build" in kindless
        assert "do not retry" in kindless
        assert "workspace files" not in kindless


class TestDisabledPythonKindNext:
    """gr753020: 14 calls from 12 sessions retried ``search(kind='python')``
    on a host with no PRECIS_PYTHON_ROOTS. The refusal now says not to
    retry, names the caller's own file tools as the way to read code, and
    offers the memory search; it no longer sends the caller to a skill
    that only explains the env var."""

    def test_python_refusal_says_do_not_retry(self, runtime: PrecisRuntime) -> None:
        assert "python" not in runtime.hub.kinds  # no PRECIS_PYTHON_ROOTS here
        out = runtime.dispatch("search", {"kind": "python", "q": "fix_gripe"})
        assert "[error:Unsupported]" in out
        assert "missing PRECIS_PYTHON_ROOTS" in out
        assert "kind 'python' is off on this host" in out
        assert "do not retry" in out
        assert "your own file and search tools" in out
        assert _next_calls(out, "search") == [
            "search(kind='memory', tags=['project:<project>'])"
        ]
        assert not _next_calls(out, "tag")
        assert "precis-kinds-disabled-help" in out
