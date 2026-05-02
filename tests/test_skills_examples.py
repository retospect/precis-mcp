"""Regression tests for shipped skill examples.

These guard against the class of MCP-critic findings where a skill's
canonical example fails at runtime — the docstring is the on-ramp, so
a wrong example is a footgun for every first-time caller.

The tests are deliberately *structural* rather than literally executing
every fenced block: many examples illustrate error paths, fictional
ids, or hypothetical APIs that can't run in isolation. Instead we
parse each fenced ``python`` block out of every shipped skill and
check the specific invariants the critic flagged (kind/axis pairing
+ python repo alias resolution).

Adding a new invariant: extend ``_check_python_block`` with another
matcher and a one-line per-violation message — the existing two
patterns illustrate the shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.store.types import _KIND_ALLOWED_AXES

_SKILLS_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "precis" / "data" / "skills"
)

# ``# rejected`` / ``# raises`` / ``# error`` / ``# not currently legal``
# / ``# would error`` markers — lines tagged like this are deliberately
# wrong; skip them rather than treating their content as canonical.
_NEGATIVE_LINE_MARKERS = (
    "# rejected",
    "# raises",
    "# error",
    "# not currently legal",
    "# would error",
)

# ``# [error:...]`` lines are doc-rendered error envelopes (showing the
# response shape), not actual calls. Skip blocks that consist of
# annotated error commentary rather than executable code.
_ERROR_RENDER_RE = re.compile(r"^\s*#\s*\[error:")

# Skill frontmatter values that mark the entire file as forward-
# looking — examples may reference unregistered tags / unimplemented
# views. These skills are filtered from the default skill index by
# the runtime, but they still ship in the data dir as design notes.
_ASPIRATIONAL_STATUSES = frozenset({"planned", "aspirational", "draft"})

# Placeholder repo aliases used in skills that teach the *shape* of
# the python-id grammar without binding to a specific repo. ``r`` is
# the canonical "your repo" stand-in throughout precis-python-help.
_PLACEHOLDER_PYTHON_ALIASES = frozenset({"r"})

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _is_aspirational(text: str) -> bool:
    """Skip aspirational / planned skills — their examples reference
    unregistered closed prefixes (``DENSITY:``, ``CONFIDENCE:``) and
    unimplemented views by design. The runtime filters them from the
    default index already; the linter does the same."""
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return False
    fm = m.group(1)
    status_match = re.search(r"^status:\s*(\S+)", fm, re.MULTILINE)
    if status_match is None:
        return False
    return status_match.group(1).strip().lower() in _ASPIRATIONAL_STATUSES


def _iter_skill_files() -> list[Path]:
    return sorted(p for p in _SKILLS_DIR.iterdir() if p.suffix == ".md")


def _extract_python_blocks(text: str) -> list[str]:
    """Return every fenced ``python`` (or ``py``) block, body only."""
    out: list[str] = []
    pattern = re.compile(r"```(?:python|py)\n(.*?)\n```", re.DOTALL)
    for m in pattern.finditer(text):
        out.append(m.group(1))
    return out


def _strip_negative_lines(block: str) -> str:
    """Drop lines that explicitly mark themselves as expected-to-fail.

    Block-style examples like::

        tag(kind='memory', id=48, add=['PRIO:high'])      # rejected

    are deliberate counter-examples; we mustn't lint them as if they
    were canonical. Lines bearing :data:`_NEGATIVE_LINE_MARKERS` (or
    matching the ``# [error:...]`` doc-render shape) are dropped.
    """
    keep: list[str] = []
    for line in block.splitlines():
        lower = line.lower()
        if any(marker in lower for marker in _NEGATIVE_LINE_MARKERS):
            continue
        if _ERROR_RENDER_RE.match(line):
            continue
        keep.append(line)
    return "\n".join(keep)


# Pull the bracket-content out of ``add=[...]`` / ``remove=[...]`` /
# ``tags=[...]`` / ``untags=[...]`` even when the list spans newlines.
# Used in conjunction with ``_KIND_RE`` per-call to attribute tags to
# their owning kind.
_KIND_RE = re.compile(r"kind=['\"](?P<kind>[a-z0-9_-]+)['\"]")
_TAGS_RE = re.compile(
    r"(?:tags|untags|add|remove)\s*=\s*\[(?P<body>[^\]]*)\]",
    re.DOTALL,
)
_CLOSED_TAG_RE = re.compile(r"['\"](?P<prefix>[A-Z]+):(?P<value>[^'\"]+)['\"]")


def _check_kind_axis_pairing(block: str, *, file: str) -> list[str]:
    """Yield error messages when a ``kind=X`` call uses a closed
    prefix the kind isn't allowed to carry.

    The matcher is intentionally local to one fenced block: cross-
    block kind reuse (e.g. setting up a kind in one block and tagging
    it in another) is rare in our skills, and treating each block as
    self-contained eliminates a swathe of false positives.
    """
    errors: list[str] = []
    cleaned = _strip_negative_lines(block)
    if "kind=" not in cleaned:
        return errors

    # Each call statement (best-effort split): a call ends at the
    # first top-level ``)`` after a ``kind=``. We split on bare
    # ``\n\n`` between calls and walk each fragment.
    fragments = re.split(r"\n\s*\n", cleaned)
    for frag in fragments:
        kind_match = _KIND_RE.search(frag)
        if not kind_match:
            continue
        kind = kind_match.group("kind")
        allowed = _KIND_ALLOWED_AXES.get(kind)
        if allowed is None:
            # Kind isn't gated — every closed prefix is fine.
            continue
        for tags_match in _TAGS_RE.finditer(frag):
            body = tags_match.group("body")
            for tm in _CLOSED_TAG_RE.finditer(body):
                prefix = tm.group("prefix")
                if prefix not in allowed:
                    errors.append(
                        f"{file}: kind={kind!r} call uses '{prefix}:' tag "
                        f"but kind {kind!r} only accepts axes {sorted(allowed)} "
                        f"(line context: {tm.group(0)!r})"
                    )
    return errors


# ``python`` ids look like ``alias::pkg.mod[.symbol]``. The known good
# aliases come from ``PRECIS_PYTHON_ROOTS`` config — the precis-mcp
# repo registers itself as ``precis``. If a skill references an alias
# outside this allowlist, it's almost certainly wrong (the MCP critic
# flagged exactly this for ``precis-mcp::``).
_KNOWN_PYTHON_ALIASES = {"precis"}
_PYTHON_ID_RE = re.compile(
    r"id=['\"](?P<alias>[a-z0-9_-]+)::(?P<rest>[a-z0-9_.]+)['\"]"
)


def _check_python_aliases(block: str, *, file: str) -> list[str]:
    """Yield error messages when ``kind='python', id='X::...'`` uses
    an alias outside :data:`_KNOWN_PYTHON_ALIASES` and is not a
    documented placeholder alias.

    The check fires only when the same fragment that pins
    ``kind='python'`` carries an ``id=`` with the ``alias::`` shape.
    Placeholder aliases (``r``) are exempt — they teach the grammar
    rather than reference a real repo.
    """
    errors: list[str] = []
    cleaned = _strip_negative_lines(block)
    if "kind='python'" not in cleaned and 'kind="python"' not in cleaned:
        return errors

    fragments = re.split(r"\n\s*\n", cleaned)
    for frag in fragments:
        if "kind='python'" not in frag and 'kind="python"' not in frag:
            continue
        for m in _PYTHON_ID_RE.finditer(frag):
            alias = m.group("alias")
            if alias in _PLACEHOLDER_PYTHON_ALIASES:
                continue
            if alias not in _KNOWN_PYTHON_ALIASES:
                errors.append(
                    f"{file}: kind='python' example uses alias {alias!r} "
                    f"(known aliases: {sorted(_KNOWN_PYTHON_ALIASES)}, "
                    f"placeholders: {sorted(_PLACEHOLDER_PYTHON_ALIASES)}); "
                    f"the precis-mcp repo is configured as 'precis'"
                )
    return errors


# ── tests ────────────────────────────────────────────────────────


def test_every_skill_kind_axis_pair_is_legal() -> None:
    """No shipped skill carries a fenced ``kind='X'`` example that
    pairs a closed-prefix tag with a kind that doesn't allow it.

    Repro of the MCP critic MAJOR-C finding: ``precis-tags`` had
    ``tag(kind='memory', add=['PRIO:high'])`` — runtime rejects with
    ``axis not allowed on kind 'memory'``. This test catches the
    same shape across every shipped skill.

    Aspirational skills (``status: planned`` / ``aspirational``) are
    skipped by design — they reference unregistered prefixes like
    ``DENSITY:`` and ``CONFIDENCE:`` to describe planned features,
    and the runtime already filters them from the default index.
    """
    errors: list[str] = []
    for path in _iter_skill_files():
        text = path.read_text(encoding="utf-8")
        if _is_aspirational(text):
            continue
        for block in _extract_python_blocks(text):
            errors.extend(_check_kind_axis_pairing(block, file=path.name))
    assert not errors, "skill examples have illegal kind/axis pairings:\n" + "\n".join(
        errors
    )


def test_every_python_kind_example_uses_known_alias() -> None:
    """No shipped skill uses a ``kind='python'`` alias outside the
    canonical set (and not also a documented placeholder).

    Repro of the MCP critic MAJOR-C finding: ``precis-overview`` had
    ``id='precis-mcp::precis.cli.main'`` — runtime answered NotFound
    (only ``precis`` is configured).
    """
    errors: list[str] = []
    for path in _iter_skill_files():
        text = path.read_text(encoding="utf-8")
        if _is_aspirational(text):
            continue
        for block in _extract_python_blocks(text):
            errors.extend(_check_python_aliases(block, file=path.name))
    assert not errors, "skill examples use unknown python aliases:\n" + "\n".join(
        errors
    )


def test_negative_marker_filter_works() -> None:
    """Sanity: lines marked with ``# rejected`` / ``# raises`` / etc.
    must be excluded from the linter (they're deliberate counter-
    examples). Without this, the per-kind axis matrix in precis-tags
    would itself trip the test."""
    block = """
tag(kind='memory', id=48, add=['prio:high'])      # OK
tag(kind='memory', id=48, add=['PRIO:high'])      # rejected
"""
    cleaned = _strip_negative_lines(block)
    # Negative line dropped; good line kept.
    assert "'prio:high'" in cleaned
    assert "'PRIO:high'" not in cleaned


def test_skills_directory_exists_and_has_content() -> None:
    """Smoke test — the loader infrastructure is shared across the
    other two tests, so make sure it actually finds skills."""
    paths = _iter_skill_files()
    assert paths, f"no skill .md files found in {_SKILLS_DIR}"
    # And every skill should have at least *something* fenced.
    has_any_python = False
    for path in paths:
        if _extract_python_blocks(path.read_text()):
            has_any_python = True
            break
    assert has_any_python, "no python-fenced examples found in any skill"


@pytest.mark.parametrize(
    "skill_name",
    [
        "precis-tags",
        "precis-overview",
    ],
)
def test_specific_critic_findings_are_fixed(skill_name: str) -> None:
    """Pin the two MAJOR-C findings as named regressions — the
    structural test above catches the class, and this one catches the
    specific instances by skill name. If a future refactor breaks the
    parser without breaking the structural test, this still fires."""
    path = _SKILLS_DIR / f"{skill_name}.md"
    assert path.exists(), f"missing skill file: {path}"
    text = path.read_text(encoding="utf-8")

    if skill_name == "precis-tags":
        # The original "Set tags" example must use a workflow kind.
        # We can't easily check the *exact* line, but assert the
        # canonical-form example carries 'todo' (or 'gripe' / 'quest')
        # rather than 'memory' near the first PRIO:high reference.
        assert "tag(kind='memory', id=48, add=[\n    'PRIO:high'" not in text, (
            "precis-tags 'Set tags' example still uses kind='memory' with PRIO:"
        )

    if skill_name == "precis-overview":
        # The python row example must not use the unknown 'precis-mcp'
        # alias. Either 'precis::' or no '::python::' identifier at all.
        assert "precis-mcp::" not in text, (
            "precis-overview python example still uses alias 'precis-mcp'"
        )
