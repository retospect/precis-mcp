"""Slice-3 recipe pins — docs/backlog/skill-graph.md "Slice 3 — recipe pinning".

The slice-2 sweep (docs/backlog/skill-sweep-flags/batch-*.md) flagged each
skill's worked examples as either `drift` (a doc/runtime mismatch — tracked
as separate content bugs, out of scope here) or `pin` (a concrete,
executable recipe worth protecting). This file pins a *selective* subset of
the `pin`-flagged entries: high-traffic, core-workflow recipes whose contract
isn't already covered by an existing handler test suite.

**A pin is a contract check, not an output-equality check.** Each test names
the skill file + section it protects in its docstring, executes (or
statically parses) the documented call shape, and asserts the invariant the
skill claims — no error, the expected keys/markers/values present. When a
future skill edit or runtime change breaks the recipe, the test reddens with
a pointer straight back to the skill section that made the claim.

Read-path / no-store examples run directly against real handler code
(no DB). Write-path examples run against the shared ``store``/``hub``
fixtures (the ephemeral Postgres test DB wired by ``tests/conftest.py``) —
never the session MCP, never prod, matching the slice-3 write-path rule.

Many `pin`-flagged entries from the sweep were investigated and found to
already have dedicated coverage elsewhere (job retry: ``test_job_retry.py``;
`uncited=` search: ``test_uncited_search.py``; material canonical-unit
rejection: ``test_material.py``; todo `view='raw'` meta dump:
``test_todo_views.py``; quest logbook entries: ``test_quest.py`` /
``test_quest_gaps.py``; toon/youtube/random contracts: their own dedicated
test files) — those are intentionally *not* duplicated here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._prio_tag import PRIO_TAG_TO_INT
from precis.handlers.finding import FindingHandler
from precis.handlers.gripe import GripeHandler
from precis.handlers.python import PythonHandler
from precis.handlers.todo import _RESERVED_PARENT_REL, TodoHandler
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry
from precis.workers.schedule.parse import every_to_cron
from tests.conftest import id_of

_SKILLS_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "precis" / "data" / "skills"
)


def _skill_text(slug: str) -> str:
    path = _SKILLS_DIR / f"{slug}.md"
    assert path.exists(), f"missing skill file: {path}"
    return path.read_text(encoding="utf-8")


# ── precis-gripe-help.md — "File a bug I just noticed" (batch-ac: pin) ────


def test_gripe_help_file_a_bug_response_shape(store: Store) -> None:
    """Pins ``precis-gripe-help.md`` ## File a bug I just noticed:

        put(kind="gripe", text="paper slug NotFound does not surface near-match options")
        # -> created gripe id=42 (STATUS:open)

    The ack line's exact shape (``created gripe id=<n> (STATUS:open).``) is
    what the doc teaches an agent to expect back from the call.
    """
    h = GripeHandler(hub=Hub(store=store))
    resp = h.put(text="paper slug NotFound does not surface near-match options")
    m = re.search(r"created gripe id=(\d+) \(STATUS:open\)\.", resp.body)
    assert m is not None, f"unexpected gripe put ack shape: {resp.body!r}"
    ref = store.get_ref(kind="gripe", id=int(m.group(1)))
    assert ref is not None


# ── precis-finding-help.md — "Register a finding..." (batch-ac: pin) ──────


def _seed_paper_chunk(store: Store, *, slug: str = "miller23a") -> str:
    """Insert a minimal paper + one body chunk; return its ``cited_in=``
    frontier handle (bare cite_key, chunk 0)."""
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Test paper {slug}", meta={})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=f"Body chunk of {slug}.", meta={})]
    )
    return slug


def test_finding_help_register_response_shape(store: Store) -> None:
    """Pins ``precis-finding-help.md`` ## Register a finding so the worker
    can chase its source: the documented ``put(kind='finding', title=,
    body=, scope=, cited_in=)`` call must execute and the ack must carry
    ``pub_id=`` and the ``placeholder: [...]`` line the doc shows — an
    agent copies that placeholder into prose while the claim is chased."""
    cite_key = _seed_paper_chunk(store)
    h = FindingHandler(hub=Hub(store=store))
    resp = h.put(
        title="gate-bias 2.4 kV / 30 s on Si/SiO2",
        body=(
            "Device prep: 2.4 kV applied across the 50 nm gate oxide "
            "for 30 s on Si/SiO2 MOSCAPs with a Cu top contact "
            "(sputtered), N2 ambient, room temp."
        ),
        scope={
            "electrode": "Cu",
            "ambient": "N2",
            "technique": "DC ramp",
            "substrate": "Si/SiO2",
        },
        cited_in=cite_key,
    )
    assert re.search(r"created finding id=\d+ pub_id=\S+", resp.body), resp.body
    assert "placeholder: [" in resp.body


# ── precis-todo-tree-help.md — prio=N / PRIO: alias (batch-ah: pin) ───────


def test_todo_tree_prio_alias_table_matches_doc() -> None:
    """Pins the alias table in ``precis-todo-tree-help.md`` ## Facet fields
    + tag vocabulary specific to the tree ("Priority is set with the
    `prio=N` kwarg... `PRIO:urgent|high|normal|low`... urgent->1 *
    high->3 * normal->5 * low->8")."""
    assert PRIO_TAG_TO_INT == {
        "PRIO:urgent": 1,
        "PRIO:high": 3,
        "PRIO:normal": 5,
        "PRIO:low": 8,
    }


def test_todo_tree_prio_precedence_and_clear(store: Store) -> None:
    """Pins the same section's precedence claims: an explicit ``prio=``
    wins over a simultaneous ``PRIO:*`` tag alias, and removing a
    ``PRIO:*`` tag clears the column back to the (unset) default."""
    h = TodoHandler(hub=Hub(store=store))
    rid = id_of(h.put(text="tree prio pin").body)

    def prio_of() -> int | None:
        ref = store.get_ref(kind="todo", id=rid)
        assert ref is not None
        return ref.prio

    h.tag(id=rid, add=["PRIO:high"])
    assert prio_of() == 3

    # Explicit prio= wins when both are passed in the same call.
    h.tag(id=rid, add=["PRIO:low"], prio=1)
    assert prio_of() == 1

    # Removing the PRIO:* tag clears the column back to the default.
    h.tag(id=rid, remove=["PRIO:low"])
    assert prio_of() is None


# ── precis-relations.md — "Which relation should I use?" (batch-af: pin) ──


_REL_TABLE_ROW_RE = re.compile(
    r"^\|\s*`([a-z][a-z-]*)`(?:\s*\(default\))?\s*\|", re.MULTILINE
)


def test_relations_table_vocabulary_matches_runtime(store: Store) -> None:
    """Pins ``precis-relations.md`` ## Which relation should I use?: every
    ``rel=`` value the table's first column documents must still be an
    accepted ``link(rel=...)`` value (vocabulary drift regression — a doc
    row for a relation the runtime dropped, or vice versa, reddens this).

    ``parent`` is the one reserved *virtual* relation (todo-tree move, no
    ``links`` row) and is checked against that constant instead of the
    link-table vocabulary.
    """
    from precis.handlers._link_tag_ops import validate_relation

    text = _skill_text("precis-relations")
    rels = _REL_TABLE_ROW_RE.findall(text)
    assert len(rels) >= 20, f"table parse found too few rows: {rels!r}"

    for rel in rels:
        if rel == "parent":
            assert rel == _RESERVED_PARENT_REL
            continue
        # Must not raise — every documented rel= is a live, accepted value.
        validate_relation(rel, store=store)


# ── precis-edit-help.md — ambiguous / not-found find= errors (batch-ab) ───


@pytest.fixture
def _edit_pin_repo(tmp_path: Path) -> Path:
    (tmp_path / "mod.py").write_text(
        '"""Fixture module for an edit-help ambiguous/not-found pin."""\n'
        "# the fox jumps over a wall\n"
        "# the morning was clear\n"
        "# the afternoon was warm\n"
        "# dopamine is a neurotransmitter\n",
        encoding="utf-8",
    )
    return tmp_path


def test_edit_help_ambiguous_match_error_shape(_edit_pin_repo: Path) -> None:
    """Pins ``precis-edit-help.md`` ## When find= matches more than once:
    ``find='the'`` with 3 matches and the default ``match='unique'``
    policy raises listing every candidate with line numbers, and naming
    the disambiguation options (``before=``/``after=``/``match=``)."""
    h = PythonHandler(hub=Hub(), roots={"r": _edit_pin_repo})
    with pytest.raises(BadInput) as excinfo:
        h.edit(id="r/mod.py", find="the", text="THE")
    msg = str(excinfo.value)
    assert "has 3 matches" in msg
    assert "match='unique' requires exactly 1" in msg
    assert "L2" in msg and "L3" in msg and "L4" in msg


def test_edit_help_not_found_error_shape(_edit_pin_repo: Path) -> None:
    """Pins ``precis-edit-help.md`` ## When find= isn't found: a literal
    that doesn't match raises naming up to 3 fuzzy nearest lines so the
    agent has something concrete to fix."""
    h = PythonHandler(hub=Hub(), roots={"r": _edit_pin_repo})
    with pytest.raises(BadInput) as excinfo:
        h.edit(id="r/mod.py", find="dpoamine", text="dopamine")
    msg = str(excinfo.value)
    assert "not found in" in msg
    assert "Nearest matches in the region:" in msg
    assert "dopamine is a neurotransmitter" in msg


# ── precis-recurring-help.md — "Schedule format" every: clamp (batch-af) ──


def test_recurring_help_every_minutes_clamp() -> None:
    """Pins ``precis-recurring-help.md`` ## Schedule format's inline
    example: ``every: '60m'`` is explicitly called out as invalid ("m
    capped at 59") while ``59m`` (and the ``1h`` equivalent it recommends)
    are accepted."""
    with pytest.raises(BadInput, match="N must be <= 59"):
        every_to_cron("60m")
    assert every_to_cron("59m") == "*/59 * * * *"
    assert every_to_cron("1h") == "0 * * * *"


# ── precis-cite-paper-help.md — the citation-router table (batch-ab) ──────


def test_cite_paper_help_router_table_handles_and_kinds_are_live() -> None:
    """Pins ``precis-cite-paper-help.md`` ## inline handle vs finding vs
    citation vs bibtex — which do I use?: the table's handle prefixes
    (``pc``/``pk``/``fi``/``me``/``dc``/``pa``) and kind names
    (``finding``/``citation``/``paper``) must still resolve in the handle
    registry — a silent prefix/kind rename would make the router's advice
    wrong without tripping any other test."""
    text = _skill_text("precis-cite-paper-help")
    assert "[pc<id>]" in text and "[pk<id>]" in text
    assert "[fi<id>]" in text and "[me<id>]" in text and "[dc<id>]" in text
    assert "kind='citation'" in text

    assert handle_registry.CHUNK_CODES["paper"] == "pc"
    assert handle_registry.CHUNK_CODES["patent"] == "pk"
    assert handle_registry.KIND_CODES["finding"] == "fi"
    assert handle_registry.KIND_CODES["memory"] == "me"
    assert handle_registry.CHUNK_CODES["draft"] == "dc"
    assert handle_registry.KIND_CODES["paper"] == "pa"
    for kind in ("citation", "finding", "paper", "memory", "draft"):
        assert handle_registry.is_known_kind(kind), f"{kind!r} no longer known"
