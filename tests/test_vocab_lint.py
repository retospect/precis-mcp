"""Vocabulary-compaction gate (``docs/backlog/vocab-compaction-stages.md``,
``docs/glossary.md``) — pure AST/text, no DB, fast.

Three independent checks:

1. Every ``→ `path` `` pointer in the glossary's Coined-terms/Overloaded
   sections resolves to a real file (a dead pointer is worse than none —
   it sends the reader nowhere and nobody notices until they follow it).
2. A reserved homonym (``Tier``/``Finding``/``Candidate``/``Hub``/``Block``/
   ``Chunk``/``ChunkRow``/``GateResult``) may only be a ``class`` name in
   its allowlisted module(s)
   — new code should coin a fresh word rather than collide with one the
   glossary already routes.
3. A retired identifier/phrase (superseded by the vocab-compaction stages)
   doesn't creep back into a new ``def``/``class`` name or a src comment.

Each vocab-compaction stage updates the allowlists below in the *same
commit* as its renames — a stage that lands a rename but forgets the gate
update just makes the corresponding entry permanently empty, which is safe
(stricter, never silently permissive).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_GLOSSARY = _ROOT / "docs" / "glossary.md"
_SRC_SKIP = {"data", "migrations", "__pycache__"}


def _src_files() -> list[Path]:
    return [
        p
        for p in _ROOT.glob("src/**/*.py")
        if not _SRC_SKIP & set(p.relative_to(_ROOT).parts)
    ]


# ── Check 1 — glossary pointers resolve ──────────────────────────────────────

# Only the "code entry-point index" sections carry file pointers. "Projects &
# quests" pointers are explicitly `todo`/`quest` ids per the section's own
# preamble ("these don't have a code home") — a `projects/<slug>` annotation
# there is a DB project tag, not a repo path, so it is out of scope here.
_POINTER_SECTION_START = "## Coined terms"
_POINTER_SECTION_END = "## Projects & quests"


def _pointer_region(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(_POINTER_SECTION_START))
    end = next(i for i, l in enumerate(lines) if l.startswith(_POINTER_SECTION_END))
    return list(enumerate(lines[start:end], start=start + 1))


def _candidate_paths(line: str) -> list[str]:
    """Backtick spans on the pointer side (after the first ``→ ``) that look
    like a repo path (contain ``/``) — a bare symbol/skill-id/module-dotted-
    name/quest-id backtick span (``promote_tiers``, `precis-fisheye-help`,
    `164903`, `precis.nanopub`) has no ``/`` and is not a pointer."""
    if "→ " not in line:
        return []
    rest = line.split("→ ", 1)[1]
    out = []
    for span in re.findall(r"`([^`]+)`", rest):
        if "/" not in span:
            continue
        path = span.split("::", 1)[0].strip()  # drop ::symbol suffix
        out.extend(_expand_braces(path))
    return out


def _expand_braces(path: str) -> list[str]:
    """``dir/{a,b,c}.py`` shorthand (one glossary entry, several files) ->
    each concrete path. A path with no ``{...}`` expands to itself."""
    m = re.search(r"\{([^}]+)\}", path)
    if not m:
        return [path]
    alts = m.group(1).split(",")
    return [path[: m.start()] + alt + path[m.end() :] for alt in alts]


def test_glossary_pointers_resolve() -> None:
    text = _GLOSSARY.read_text(encoding="utf-8")
    missing: list[str] = []
    for lineno, line in _pointer_region(text):
        for cand in _candidate_paths(line):
            if not (_ROOT / cand).exists():
                missing.append(f"glossary.md:{lineno} → {cand}")
    assert not missing, (
        "glossary pointer(s) resolve to nothing (rot: the term's home moved "
        "and the pointer wasn't updated in the same commit):\n" + "\n".join(missing)
    )


def test_glossary_pointer_extraction_finds_real_pointers() -> None:
    """Self-check: an empty result would make the check above vacuous."""
    text = _GLOSSARY.read_text(encoding="utf-8")
    found = [c for _, line in _pointer_region(text) for c in _candidate_paths(line)]
    assert len(found) > 50, "pointer region under-matched — regex likely broken"


# ── Check 2 — reserved homonyms stay in their allowlisted module(s) ─────────

# term -> repo-relative modules allowed to define `class <term>`. Anywhere
# else, the class must coin a fresh word (see docs/glossary.md "Overloaded").
# Each vocab-compaction stage updates these lists in the same commit as its
# renames — do not add a new offender here to make a red gate green.
_RESERVED_CLASS_ALLOWLIST: dict[str, set[str]] = {
    "Tier": {"src/precis/utils/llm/router.py"},
    "Finding": set(),  # the `finding` ref kind owns the word outright
    "Candidate": {"src/precis/quest/frontier.py"},
    "Hub": {"src/precis/dispatch.py"},
    # stage B (store.blocks -> chunks) renamed the store type to `ChunkRow`;
    # prompt Block is a separate pending rename decision, not folded in.
    "Block": {"src/precis/utils/prompt/model.py"},
    # parse-fragment types, pre-existing (not part of stage B's chunk facade
    # rename — see docs/backlog/vocab-compaction-stages.md stage B note).
    "Chunk": {
        "src/precis/skill_index/chunker.py",
        "src/precis/draftimport/tex.py",
    },
    "ChunkRow": {"src/precis/store/types.py"},
    # known pair, grandfathered — a future pass splits narrative_budget's
    # rewrite-gate result from the python-write-tool's edit-gate result.
    "GateResult": {
        "src/precis/quest/narrative_budget.py",
        "src/precis/handlers/_python_write.py",
    },
}


def test_reserved_class_names_stay_in_their_allowlisted_modules() -> None:
    bad: list[str] = []
    reserved = set(_RESERVED_CLASS_ALLOWLIST)
    for path in _src_files():
        try:
            tree = ast.parse(path.read_bytes())
        except SyntaxError:
            continue
        rel = path.relative_to(_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in reserved:
                if rel not in _RESERVED_CLASS_ALLOWLIST[node.name]:
                    bad.append(f"{rel}:{node.lineno} class {node.name}")
    assert not bad, (
        "reserved/overloaded class name used outside its glossary-allowlisted "
        "module(s) — see docs/glossary.md ('Overloaded — which one?') for the "
        "word this homonym already owns; coin a different name instead:\n"
        + "\n".join(sorted(bad))
    )


def test_reserved_class_allowlist_entries_are_still_current() -> None:
    """Self-check the other direction: an allowlisted module that no longer
    defines the class is stale — trim it in the same commit as the rename
    that moved/removed it (a stale allowlist entry silently widens the gate
    for the next unrelated class to reuse that slot)."""
    defined: dict[str, set[str]] = {name: set() for name in _RESERVED_CLASS_ALLOWLIST}
    for path in _src_files():
        try:
            tree = ast.parse(path.read_bytes())
        except SyntaxError:
            continue
        rel = path.relative_to(_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in defined:
                defined[node.name].add(rel)
    stale = {
        name: sorted(allowed - defined[name])
        for name, allowed in _RESERVED_CLASS_ALLOWLIST.items()
        if allowed - defined[name]
    }
    assert not stale, (
        f"stale reserved-class allowlist entries (class moved/removed): {stale}"
    )


# ── Check 3 — retired names/phrases don't creep back ────────────────────────

# Bare def/class names retired by a vocab-compaction stage. An allowlist
# entry here is a *known, permanent, unrelated* homonym only — not a
# parking spot for a straggler that should just be renamed.
_RETIRED_NAMES = {
    "PassBand",
    "format_patent_citation",
    "_extract_json",
    # Stage C persisted-key renames (docs/backlog/vocab-compaction-stages.md):
    # these were never a def/class name themselves (dataclass fields / dict
    # keys / DB columns), so this only catches a *new* def/class reusing the
    # bare word — belt-and-suspenders alongside the migrations that renamed
    # the persisted keys.
    "tier_ladder",
    "barrier_tier",
    "tier_tag",
    "claim_ref_id",
    "PRECIS_BACKFILL_CITATION_LENS",
    # Stage D surface renames (docs/backlog/vocab-compaction-stages.md):
    # `_dispatch_pass` (cli/worker.py closure) -> `_minter_pass`, matching
    # the registry rename `dispatch` -> `minter`; `block_pos`/`block_slug`
    # (utils/file_id.py::format_write_result kwargs) -> `chunk_pos`/
    # `chunk_slug` -- never def/class names themselves, belt-and-suspenders
    # per the Stage C pattern above.
    "_dispatch_pass",
    "block_pos",
    "block_slug",
    # Stage E surface renames (docs/backlog/vocab-compaction-stages.md):
    # the web Tasks-tab route module's helpers renamed with its
    # `/tasks` -> `/todo` route (`routes/todo.py`); `Store.soft_delete_ref`
    # -> `retire_ref` (+ the same-shaped `soft_delete_todo_subtree` /
    # `soft_delete_draft`) and the `refs.deleted_at` column -> `retired_at`
    # -- the column/field is never a def/class name itself, belt-and-
    # suspenders per the Stage C pattern above.
    "_tasks_url",
    "task_pdf",
    "soft_delete_ref",
    "soft_delete_todo_subtree",
    "soft_delete_draft",
    "deleted_at",
}
# name-pattern -> (regex, files exempted as a genuine unrelated word sense).
# "glossary" itself must not trip the gloss->summary retirement.
_GLOSS_STYLE = re.compile(r"^_?gloss(_|$)")
_GLOSS_STYLE_ALLOW = {
    # "to gloss a quote" = annotate it with hover definitions -- the English
    # verb, unrelated to the retired `gloss` one-line-description field.
    "src/precis_web/claim_render.py::_gloss_quote",
}
# bare `dispatch` def is retired only inside the LLM router package (its
# entrypoint renamed to `route`); `dispatch` is a legitimate name everywhere
# else in the repo (the Hub verb table, the worker, runtime.dispatch, ...).
_ROUTER_DIR = "src/precis/utils/llm/"


def _retired_identifier_hits(path: Path, tree: ast.AST) -> list[str]:
    rel = path.relative_to(_ROOT).as_posix()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = node.name
        if name in _RETIRED_NAMES:
            out.append(f"{rel}:{node.lineno} {name}")
        elif _GLOSS_STYLE.match(name) and name != "glossary":
            if f"{rel}::{name}" not in _GLOSS_STYLE_ALLOW:
                out.append(f"{rel}:{node.lineno} {name}")
        elif (
            name == "dispatch"
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and rel.startswith(_ROUTER_DIR)
        ):
            out.append(f"{rel}:{node.lineno} {name} (router entry is `route`)")
    return out


def test_retired_identifiers_do_not_reappear() -> None:
    bad: list[str] = []
    for path in _src_files():
        try:
            tree = ast.parse(path.read_bytes())
        except SyntaxError:
            continue
        bad += _retired_identifier_hits(path, tree)
    assert not bad, (
        "retired vocab-compaction identifier reused as a new def/class name "
        "(see docs/glossary.md):\n" + "\n".join(sorted(bad))
    )


# Phrases retired in favour of a glossary term. Comments/docstrings only —
# scanning code would false-positive on unrelated identifiers/strings.
# "kill switch" is deliberately NOT here: it names a real, distinct pattern
# (classify_topics force-off, the self-healing-spine design) and is not a
# synonym for anything vocab-compaction retired.
_RETIRED_PHRASES = [
    "env gate",
    "env-gate",
    "dark flag",
    "trust tier",
    "the blocks table",
    "verified-by-refine",
    # Stage D surface renames (docs/backlog/vocab-compaction-stages.md):
    # the dispatch-worker skill id, and the patent/edgar search-leg kwarg
    # (now `precis-minter-help` / `reach='remote'`).
    "precis-dispatch-help",
    "source='remote'",
    'source="remote"',
    # Stage E surface renames (docs/backlog/vocab-compaction-stages.md):
    # task->todo (the web Tasks tab, the "task line"/`text=` title, the
    # tree skill's old id) and the retire/soft-delete unification (bare
    # "task"/"deleted_at" are NOT banned here -- both collide too heavily
    # with generic English / other tables' history to be a deterministic
    # phrase gate; the identifier-level bans above are the enforcement).
    "precis-tasks-help",
    "precis-auto-tasks-help",
    "tasks tab",
    "task line",
]
_PHRASE_RE = re.compile("|".join(re.escape(p) for p in _RETIRED_PHRASES), re.IGNORECASE)


def _comment_and_docstring_text(tree: ast.Module, source: str) -> list[tuple[int, str]]:
    out = [
        (i, line)
        for i, line in enumerate(source.splitlines(), start=1)
        if "#" in line and line.lstrip().startswith("#")
    ]
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc:
            base = node.body[0].lineno if node.body else getattr(node, "lineno", 1)
            for off, dline in enumerate(doc.splitlines()):
                out.append((base + off, dline))
    return out


def test_retired_phrases_do_not_reappear_in_src_comments() -> None:
    bad: list[str] = []
    for path in _src_files():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel = path.relative_to(_ROOT).as_posix()
        for lineno, text in _comment_and_docstring_text(tree, source):
            m = _PHRASE_RE.search(text)
            if m:
                bad.append(f"{rel}:{lineno} {m.group(0)!r}")
    assert not bad, (
        "retired phrase found in a src comment/docstring — use the "
        "glossary's current term instead:\n" + "\n".join(sorted(bad))
    )


def test_retired_phrase_regex_matches_its_own_samples() -> None:
    """Self-check: each banned phrase's regex actually fires (a typo'd
    pattern would make the guard above vacuously green)."""
    for phrase in _RETIRED_PHRASES:
        assert _PHRASE_RE.search(f"# this uses the {phrase} pattern"), phrase
    assert not _PHRASE_RE.search("# a genuine kill switch, not an env var")
