"""Python-kind search: per-term lexical scoring, pattern ranking."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.python import PythonHandler


def _write(repo: Path, rel: str, content: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def handler(tmp_path: Path) -> PythonHandler:
    _write(tmp_path, "pkg/__init__.py", '"""pkg."""\n')
    _write(
        tmp_path,
        "pkg/fetch.py",
        '''
        def safe_get(url):
            """``client.get(url)`` with SSRF-validated, IP-pinned redirects."""
            return url
        ''',
    )
    _write(
        tmp_path,
        "pkg/prov.py",
        '''
        def corpus_fingerprint(x):
            """Hash of the indexed corpus."""
            return x

        def unrelated(x):
            """Mentions provenance only in a docstring."""
            return x
        ''',
    )
    _write(
        tmp_path,
        "pkg/a_first.py",
        '''
        def zzz():
            """Talks about provenance."""
        ''',
    )
    _write(tmp_path, "pkg/provenance.py", "def stamp():\n    pass\n")
    _write(tmp_path, "tests/test_provenance.py", "def test_provenance():\n    pass\n")
    return PythonHandler(hub=Hub(), roots={"r": tmp_path})


def _handles(body: str) -> list[str]:
    return re.findall(r"^## (\S+)", body, flags=re.M)


def test_multiword_any_term_with_stemming(handler: PythonHandler) -> None:
    body = handler.search(q="SSRF redirect pinning").body
    assert "r::pkg.fetch.safe_get" in _handles(body)


def test_more_terms_matched_ranks_higher(handler: PythonHandler) -> None:
    handles = _handles(handler.search(q="indexed corpus fingerprint").body)
    assert handles[0] == "r::pkg.prov.corpus_fingerprint"


def test_exact_qualname_still_first(handler: PythonHandler) -> None:
    handles = _handles(handler.search(q="pkg.fetch.safe_get").body)
    assert handles[0] == "r::pkg.fetch.safe_get"


def test_camel_and_snake_tokenised(handler: PythonHandler) -> None:
    handles = _handles(handler.search(q="CorpusFingerprint").body)
    assert "r::pkg.prov.corpus_fingerprint" in handles


def test_pattern_ranks_name_over_docstring_and_tests(handler: PythonHandler) -> None:
    body = handler.search(q="provenance", mode="pattern", page_size=50).body
    handles = _handles(body)
    scores = [float(x) for x in re.findall(r"score=([\d.]+)", body)]
    assert len(set(scores)) > 1
    assert scores == sorted(scores, reverse=True)
    # name/qualname matches precede the test module hit
    assert handles.index("r::pkg.provenance") < handles.index(
        "r::test_provenance.test_provenance"
    )


def test_multi_term_headline_splits_all_vs_some_terms(handler: PythonHandler) -> None:
    # safe_get matches all three terms; corpus_fingerprint matches only "corpus".
    body = handler.search(q="SSRF pinned corpus").body
    assert "0 match all 3 terms" in body
    body = handler.search(q="SSRF redirect pinning").body
    assert re.search(r"^1 match all 3 terms; \d+ match only some\.$", body, flags=re.M)


def test_single_term_headline_has_no_split(handler: PythonHandler) -> None:
    assert "match all" not in handler.search(q="provenance").body


def test_test_in_query_or_scope_disables_demotion(handler: PythonHandler) -> None:
    from precis.handlers.python import _wants_tests

    assert _wants_tests("fix test flake", None, None)  # "test" in the query
    assert _wants_tests("provenance", "tests/test_provenance.py", None)  # test file
    assert not _wants_tests("provenance", "pkg/provenance.py", None)

    def score(body: str) -> float:
        m = re.search(
            r"^## r::test_provenance\.test_provenance\b.*?score=([\d.]+)",
            body,
            flags=re.M,
        )
        assert m, body
        return float(m[1])

    plain = handler.search(q="provenance", mode="pattern", page_size=50).body
    scoped = handler.search(
        q="provenance", scope="r/tests/test_provenance.py", mode="pattern"
    ).body
    assert score(scoped) > score(plain)  # demotion factor lifted by the scope


def test_nested_def_ranks_below_top_level(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def outer():
            def widget_inner():
                return 1
            return widget_inner

        def widget_top():
            return 2
        """,
    )
    h = PythonHandler(hub=Hub(), roots={"r": tmp_path})
    handles = _handles(h.search(q="widget", mode="pattern", page_size=50).body)
    top = next(i for i, x in enumerate(handles) if x.endswith("widget_top"))
    nested = next(i for i, x in enumerate(handles) if x.endswith("widget_inner"))
    assert top < nested
