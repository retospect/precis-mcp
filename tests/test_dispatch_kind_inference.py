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
``Unsupported`` naming the disabled-kinds skill and the host that reads
it, instead of bouncing on the bare "missing kind=". Precedence:
real ref slugs are checked first — a slug is an address, a kind word is
not. ``put``/``edit`` stay kind-required (get-only inference).
"""

from __future__ import annotations

import re

import pytest

from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.runtime import PrecisRuntime
from precis.store import Store

DOI = "10.1021/ja01019a011"


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
        """A bare DOI routes to the paper handler (unheld DOIs queue
        an ingest job — gr551840); the full DOI stays on id=."""
        out = runtime.dispatch("get", {"id": DOI})
        assert "[error" not in out
        assert "missing kind" not in out
        assert f"for {DOI}" in out

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
