"""Block lint for the skill corpus — block-navigation slice 1.

Designed content is served addressable: ``get(kind='skill', id='slug~N')``
returns the Nth H2 block of the scope-filtered body
(``SkillHandler._render_skill_chunks`` → ``scope_render`` →
``chunk_by_h2``). This test measures the same blocks the handler serves —
same splitter, same (write) scope: markers are stripped, fenced bodies are
full, and chunk indices are scope-invariant (gr512233). Tokens come from
the repo's estimator, ``precis.utils.llm.admit.estimate_tokens`` (chars/4).

Gates (fail):

- a block over the ~4k-token hard cap, unless its slug sits on the
  shrink-only ``_BLOCK_CAP_ALLOWLIST`` (docs/backlog/block-navigation.md:
  designed blocks target 500–2000 tokens, hard cap ~4k);
- heading-structure breaks — more than one H1, a skipped heading level,
  an empty section outside the chunker's alias groups;
- dead xrefs — every ``get(kind='skill', id=…)``, ``[[wikilink]]``,
  ``skill:slug`` handle and ``slug~N``/``slug~A..B`` address must resolve
  to a real slug, and to a real block when ``~N`` is given. Xref checks
  run on the raw file, fenced code included: a ``slug~N`` inside an
  example fence is a promise to the reader and must resolve exactly like
  a prose mention.

Report-only (warn): blocks over the 500–2000-token target, and files
carrying many sub-~150-token blocks (over-splitting — a powder of trivia
blocks costs hops instead of residency). The summary rides
``warnings.warn``, same channel as ``test_skill_size``.
"""

from __future__ import annotations

import re
import warnings
from functools import cache
from pathlib import Path

import pytest

from precis.handlers.skill import _SKILL_PATH_VIEWS, SkillHandler
from precis.skill_index.chunker import _strip_front_matter, chunk_by_h2
from precis.utils.llm.admit import estimate_tokens
from precis.utils.skill_common import WIKILINK_RE, parse_frontmatter
from precis.utils.skill_scope import render as scope_render
from tests._skill_roots import skill_files

#: Designed-block budget in tokens (block-navigation rulings). Over the
#: target is a warn, over the cap is a fail unless grandfathered.
BLOCK_TARGET_HI_TOKENS = 2000
BLOCK_HARD_CAP_TOKENS = 4000

#: A block under this is a stub, not a section — the over-splitting
#: signal the lint watches for.
BLOCK_SMALL_TOKENS = 150

#: One or two short nav sections (`## See also`) are normal; a file
#: needs this many small blocks before it counts as over-split.
SMALL_BLOCKS_MANY = 3

#: slug -> justification. Grandfather list for the ~4k-token block cap:
#: entries may only leave it (the ratchet test fails a slug whose
#: chunks all fit again); new violations fail outright.
_BLOCK_CAP_ALLOWLIST: dict[str, str] = {}

#: Bare ``get(`` calls only — the lookbehind keeps lookalike names that
#: happen to end in ``get`` (``budget(``, ``target(``, ``forget(``) and
#: method calls (``session.get(``) out of the xref check.
_GET_CALL_RE = re.compile(r"(?<![\w.])get\([^)]*\)")
_KIND_SKILL_RE = re.compile(r"kind\s*=\s*['\"]skill['\"]")
_ID_ARG_RE = re.compile(r"\bid\s*=\s*['\"]([^'\"]+)['\"]")
_SKILL_HANDLE_RE = re.compile(r"(?<![\w-])skill:([a-z0-9][a-z0-9-]*)")
_CHUNK_REF_RE = re.compile(r"(?<![\w-])([a-z0-9][a-z0-9-]*)~(\d+)(?:\.\.(\d+))?")
_SKILL_ID_GRAMMAR_RE = re.compile(
    r"^(?P<slug>[a-z0-9][a-z0-9-]*)"
    r"(?:~(?P<lo>\d+)(?:\.\.(?P<hi>\d+))?)?"
    r"(?:/(?P<view>[a-z]+))?$"
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")


#: Floor on the swept corpus — parametrization over an empty glob
#: collects nothing, so a misrooted corpus would pass vacuously. Well
#: under the real count (~220 built-ins, 2026-10) to leave room for
#: plugin-split moves.
SKILL_CORPUS_MIN_FILES = 100


def _skill_files() -> list[Path]:
    return skill_files(recursive=False)


def test_skill_corpus_is_non_empty() -> None:
    """The parametrize sweep only works on a corpus that exists."""
    files = _skill_files()
    assert len(files) >= SKILL_CORPUS_MIN_FILES, (
        f"only {len(files)} skill files found (floor "
        f"{SKILL_CORPUS_MIN_FILES}) — an empty or misrooted corpus sweeps "
        "nothing and every parametrized check passes vacuously"
    )


def _rendered(path: Path) -> str:
    """The write-scope serve body: markers stripped, fenced bodies full."""
    return scope_render(path.read_text(encoding="utf-8"), "write")[0]


@cache
def _all_skill_paths() -> dict[str, Path]:
    """stem -> path over the whole corpus (personas, plugins included)."""
    return {p.stem: p for p in skill_files(recursive=True)}


def _valid_slugs() -> frozenset[str]:
    """Everything ``id=`` may resolve: file stems + synthesised meta-skills."""
    return frozenset(
        _all_skill_paths()
        | SkillHandler._SYNTHESIZED_SKILLS
        | SkillHandler._SYNTH_ALIASES
    )


@cache
def _chunk_count(slug: str) -> int | None:
    """Structural ``~N`` block count for a file-backed slug; None for synth."""
    path = _all_skill_paths().get(slug)
    if path is None:
        return None
    return len(chunk_by_h2(_rendered(path)))


def _headings(text: str) -> list[tuple[int, str, int]]:
    """ATX headings of the rendered body — ``(level, text, lineno)``.

    Front matter is stripped and fenced code skipped: a ``# comment``
    inside ``` fences is code, not a heading (skill_scope.py).
    """
    out: list[tuple[int, str, int]] = []
    fenced = False
    for lineno, line in enumerate(_strip_front_matter(text).splitlines()):
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2), lineno))
    return out


def _alias_member_headings(path: Path) -> set[str]:
    """Individual headings of chunker alias groups — stacked ``##`` lines
    sharing one body are a section, not a run of empty sections."""
    members: set[str] = set()
    for chunk in chunk_by_h2(_rendered(path)):
        members.update(chunk.aliases)
    return members


# ── (a) block size ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.stem)
def test_skill_blocks_under_token_cap(path: Path) -> None:
    if path.stem in _BLOCK_CAP_ALLOWLIST:
        return
    over = [
        (i, estimate_tokens(len(chunk.text)), chunk.heading)
        for i, chunk in enumerate(chunk_by_h2(_rendered(path)))
        if estimate_tokens(len(chunk.text)) > BLOCK_HARD_CAP_TOKENS
    ]
    assert not over, (
        f"{path.stem} has {len(over)} block(s) over the "
        f"~{BLOCK_HARD_CAP_TOKENS}-token cap (e.g. ~{over[0][0]} "
        f"{over[0][1]} tokens {over[0][2]!r}) — designed blocks target "
        "500–2000 tokens; split the section or link the detail out "
        "(`docs/backlog/block-navigation.md`). If it genuinely cannot "
        "shrink short-term, add the slug to _BLOCK_CAP_ALLOWLIST with "
        "a justification."
    )


def test_block_cap_allowlist_is_shrink_only() -> None:
    """A grandfathered slug whose blocks all fit again must leave the
    list — the allowlist only shrinks."""
    paths = _all_skill_paths()
    for slug in _BLOCK_CAP_ALLOWLIST:
        assert slug in paths, (
            f"allowlisted skill {slug} no longer exists; remove it from the list"
        )
    stale = []
    for slug in _BLOCK_CAP_ALLOWLIST:
        path = paths[slug]
        chunks = chunk_by_h2(_rendered(path))
        if all(estimate_tokens(len(c.text)) <= BLOCK_HARD_CAP_TOKENS for c in chunks):
            stale.append(slug)
    assert not stale, (
        f"{sorted(stale)} now fit the {BLOCK_HARD_CAP_TOKENS}-token block "
        "cap — remove them from _BLOCK_CAP_ALLOWLIST"
    )


def test_block_cap_allowlist_entries_have_justification() -> None:
    for slug, justification in _BLOCK_CAP_ALLOWLIST.items():
        assert justification and justification.strip(), (
            f"_BLOCK_CAP_ALLOWLIST entry for {slug!r} must carry a "
            "non-empty justification"
        )


def test_block_size_report() -> None:
    """Drift summary, not a gate: over-target blocks and over-split files."""
    over: list[tuple[str, int, int, str]] = []
    under: list[tuple[str, int]] = []
    for path in _skill_files():
        small = 0
        for i, chunk in enumerate(chunk_by_h2(_rendered(path))):
            tokens = estimate_tokens(len(chunk.text))
            if tokens > BLOCK_TARGET_HI_TOKENS:
                over.append((path.stem, i, tokens, chunk.heading))
            if chunk.heading and tokens < BLOCK_SMALL_TOKENS:
                small += 1
        if small >= SMALL_BLOCKS_MANY:
            under.append((path.stem, small))
    if not over and not under:
        return
    parts = []
    if over:
        listing = "; ".join(
            f"{slug}~{i} ({tokens}tok {heading!r})"
            for slug, i, tokens, heading in over[:20]
        )
        parts.append(
            f"{len(over)} block(s) over the {BLOCK_TARGET_HI_TOKENS}-token "
            f"target: {listing}{' …' if len(over) > 20 else ''}"
        )
    if under:
        under.sort(key=lambda kv: -kv[1])
        listing = ", ".join(f"{slug} ({n})" for slug, n in under[:20])
        parts.append(
            f"{len(under)} skill(s) carry >={SMALL_BLOCKS_MANY} blocks under "
            f"~{BLOCK_SMALL_TOKENS} tokens (over-splitting): {listing}"
            f"{' …' if len(under) > 20 else ''}"
        )
    warnings.warn(
        "; ".join(parts)
        + ". Report-only — the gates are the byte cap (test_skill_size) "
        "and the ~4k-token block cap.",
        stacklevel=1,
    )


# ── (b) heading structure ──────────────────────────────────────────


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.stem)
def test_skill_single_top_heading(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    h1s = [txt for lvl, txt, _ in _headings(_rendered(path)) if lvl == 1]
    assert len(h1s) <= 1, f"{path.stem} has {len(h1s)} H1 headings: {h1s}"
    if not h1s:
        # Flat skills (section styles, mermaid sheets) carry the title in
        # front matter instead of an H1.
        assert parse_frontmatter(raw).title, (
            f"{path.stem} has no H1 heading and no front-matter `title:` — "
            "a block address needs a name."
        )


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.stem)
def test_skill_no_skipped_heading_levels(path: Path) -> None:
    prev = 0
    for lvl, txt, lineno in _headings(_rendered(path)):
        if prev and lvl > prev + 1:
            pytest.fail(
                f"{path.stem}:{lineno} heading level skips {prev} -> {lvl} "
                f"({txt!r}) — add the missing level or flatten"
            )
        prev = lvl


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.stem)
def test_skill_no_empty_sections(path: Path) -> None:
    served = _rendered(path)
    body_lines = _strip_front_matter(served).splitlines()
    heads = _headings(served)
    aliases = _alias_member_headings(path)
    for i, (lvl, txt, lineno) in enumerate(heads):
        if txt in aliases:
            continue
        if i + 1 < len(heads):
            nlvl, _ntxt, nlineno = heads[i + 1]
            if nlvl > lvl:
                continue  # subsection content — the section isn't empty
            if not "\n".join(body_lines[lineno + 1 : nlineno]).strip():
                pytest.fail(
                    f"{path.stem}:{lineno + 1} `{txt!r}` is an empty "
                    "section — drop it or give it a body"
                )
        elif not "\n".join(body_lines[lineno + 1 :]).strip():
            pytest.fail(
                f"{path.stem}:{lineno + 1} `{txt!r}` is a dangling empty "
                "heading at EOF — drop it or give it a body"
            )


# ── (c) link validity ──────────────────────────────────────────────


def _id_problems(text: str) -> list[str]:
    """Every ``id=`` inside a ``get(kind='skill', …)`` call must resolve."""
    valid = _valid_slugs()
    problems: list[str] = []
    for call in _GET_CALL_RE.finditer(text):
        if not _KIND_SKILL_RE.search(call.group(0)):
            continue
        m = _ID_ARG_RE.search(call.group(0))
        if m is None:
            continue
        rid = m.group(1)
        # Placeholders (`<slug>`, `precis-<kind>-help`) and the docs'
        # literal `slug`/`slug~N` meta-names aren't xrefs.
        if "<" in rid or ">" in rid or rid.split("~")[0].split("/")[0] == "slug":
            continue
        g = _SKILL_ID_GRAMMAR_RE.match(rid)
        if g is None:
            problems.append(f"unparseable skill id {rid!r}")
            continue
        slug = g.group("slug")
        if slug not in valid:
            problems.append(f"id={rid!r} names no skill")
            continue
        view = g.group("view")
        if view is not None and view not in _SKILL_PATH_VIEWS:
            problems.append(f"id={rid!r} uses unknown view /{view}")
        if g.group("lo") is not None:
            lo = int(g.group("lo"))
            hi = int(g.group("hi") or g.group("lo"))
            if hi < lo:
                problems.append(f"id={rid!r}: inverted range ~{lo}..{hi}")
                continue
            n = _chunk_count(slug)
            if n is None:
                problems.append(f"id={rid!r}: {slug} is synthesised — no ~N blocks")
            elif hi >= n:
                problems.append(f"id={rid!r}: {slug} has {n} blocks")
    return problems


def _mention_problems(text: str) -> list[str]:
    """``slug~N`` / ``skill:slug`` / ``[[slug]]`` mentions outside get().

    A bare ``~N`` ref is only checked when its slug is a real skill —
    ``pa5~15..29`` is a paper address in the same grammar, not a skill
    xref.
    """
    valid = _valid_slugs()
    problems: list[str] = []
    for m in _CHUNK_REF_RE.finditer(text):
        slug = m.group(1)
        if slug not in valid:
            continue
        n = _chunk_count(slug)
        hi = int(m.group(3) or m.group(2))
        if n is None:
            problems.append(f"{m.group(0)}: {slug} is synthesised — no ~N blocks")
        elif hi >= n:
            problems.append(f"{m.group(0)}: {slug} has {n} blocks")
    for m in _SKILL_HANDLE_RE.finditer(text):
        if m.group(1) not in valid:
            problems.append(f"skill:{m.group(1)} names no skill")
    for m in WIKILINK_RE.finditer(text):
        if m.group(1) not in valid:
            problems.append(f"[[{m.group(1)}]] names no skill")
    return problems


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.stem)
def test_skill_links_resolve(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    problems = _id_problems(text) + _mention_problems(text)
    assert not problems, (
        f"{path.stem} has {len(problems)} dead skill xref(s): "
        + "; ".join(sorted(set(problems))[:10])
    )


def test_get_call_re_ignores_lookalike_names() -> None:
    """``budget(``/``target(``/``x.get(`` aren't the ``get(`` handler."""
    text = (
        "budget(kind='skill', id='no-such-skill') "
        "target(kind='skill', id='also-none') "
        "session.get(kind='skill', id='nor-this')"
    )
    assert _id_problems(text) == []


def test_fenced_chunk_ref_must_still_resolve() -> None:
    """A ``slug~N`` inside a fenced example is checked like prose —
    illustrative or not, it is a promise to the reader."""
    slug = sorted(_all_skill_paths())[0]
    n = _chunk_count(slug)
    assert n is not None
    problems = _mention_problems(f"```\nsee {slug}~{n}\n```")
    assert problems == [f"{slug}~{n}: {slug} has {n} blocks"]
    assert _mention_problems(f"```\nsee {slug}~0\n```") == []
