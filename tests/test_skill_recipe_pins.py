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

import ast
import json
import re
from pathlib import Path
from typing import Any

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
from precis_se import persist
from precis_se import printgroup as se_printgroup
from precis_se.handler import SeHandler
from tests.conftest import id_of
from tests.test_se_print_intent import _3mf_objects, handler, register_se_simp
from tests.test_se_print_manufacture import _cut_bore
from tests.test_se_print_views import _mint_slug

# fixtures re-exported for pytest (test_se_print_manufacture.py's pattern)
__all__ = ["handler", "register_se_simp"]

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


# ── precis-se-print-help.md — §1b / §2d / §2e (gr450093 slug-free rewrite) ─
#
# The rewrite that made these sections slug-free (skills must never name a
# live prod design) also made them self-contained, valid-python-literal
# ``ops=[...]`` snippets — this pin runs each one verbatim, straight out of
# the shipped skill text, rather than re-deriving a paraphrase of it that
# could quietly drift from what the doc actually shows.


def _skill_section(slug: str, heading: str) -> str:
    """The skill's markdown between ``## <heading>`` and the next ``## ``
    heading (or EOF)."""
    text = _skill_text(slug)
    pattern = re.compile(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL
    )
    m = pattern.search(text)
    assert m is not None, f"missing '## {heading}' section in {slug}.md"
    return m.group(1)


def _extract_ops_lists(section: str) -> list[list[dict[str, Any]]]:
    """Every ``ops=[...]`` payload in a skill section's fenced python, in
    order, ``ast.literal_eval``'d out of the raw text — the skill writes
    them as valid Python literals (``True``/``None``, not ``true``/``null``)
    for exactly this reason."""
    out: list[list[dict[str, Any]]] = []
    i = 0
    while True:
        start = section.find("ops=[", i)
        if start == -1:
            break
        bracket_start = start + len("ops=")
        depth = 0
        j = bracket_start
        while j < len(section):
            if section[j] == "[":
                depth += 1
            elif section[j] == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(ast.literal_eval(section[bracket_start : j + 1]))
        i = j + 1
    assert out, "no ops=[...] payload found in section"
    return out


def test_print_help_1b_simp_realize_enqueues_and_reads_unrealized(
    handler: SeHandler, register_se_simp: Any
) -> None:
    """Pins ``precis-se-print-help.md`` ## 1b — ``realize(strategy='simp')``:
    the op only validates and enqueues an ``se_simp`` job (never solves
    inline), so the response carries a job handle and the block still reads
    ``unrealized`` in ``view='print'`` until the job lands."""
    (ops,) = _extract_ops_lists(
        _skill_section(
            "precis-se-print-help",
            "1b — `realize(strategy='simp')`: solve the material instead of seeding it",
        )
    )
    resp = handler.put(id="skillpin-1b", text=json.dumps({"ops": ops}))
    assert "se_simp" in resp.body and "enqueued" in resp.body
    body = handler.get(id="skillpin-1b", view="print", args={"block": "fork"}).body
    assert "unrealized" in body


def test_print_help_2d_model_group_renders_two_members_and_exports_two_objects(
    handler: SeHandler, hub: Hub, tmp_path: Path
) -> None:
    """Pins ``precis-se-print-help.md`` ## 2d — print groups:
    ``intent='model'``: the sketched ``assy`` ⊃ ``clamp``/``bolt`` group
    (``rail`` stays outside it) renders a print-group section naming both
    members and exports one 3MF with exactly 2 objects."""
    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    (ops,) = _extract_ops_lists(
        _skill_section(
            "precis-se-print-help",
            "2d — print groups: `intent='model'` on an ancestor block",
        )
    )
    for op in ops:
        if op.get("op") == "set_binding" and op.get("kind") == "component":
            op["design"] = screw_slug
    handler.put(id="skillpin-2d", text=json.dumps({"ops": ops}))
    handler.edit(
        id="skillpin-2d", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    ref = handler.store.get_ref(kind="se", id="skillpin-2d")
    assert ref is not None
    tree = persist.load_tree(handler.store, ref.id)
    report = se_printgroup.report_for(tree, "assy", cad_store_reader=handler.store)
    assert report is not None and report.intent == "model"
    assert {m.block for m in report.members} == {"bolt", "clamp"}

    body = handler.get(id="skillpin-2d", view="print", args={"block": "assy"}).body
    assert "print group" in body and "intent model" in body

    handler.edit(
        id="skillpin-2d",
        ops=[{"op": "set_build_frame", "block": "assy", "down": [0, 0, -1]}],
    )
    out = tmp_path / "assy.3mf"
    resp = handler.get(
        id="skillpin-2d",
        view="print",
        args={"block": "assy", "fmt": "3mf", "path": str(out)},
    )
    assert out.exists() and "2 object(s)" in resp.body
    objects = _3mf_objects(out)
    assert set(objects) == {"bolt", "clamp"}


def test_print_help_2e_manufacture_group_exports_two_objects(
    handler: SeHandler, tmp_path: Path
) -> None:
    """Pins ``precis-se-print-help.md`` ## 2e — print groups:
    ``intent='manufacture'``: the sketched pin-in-knuckle hinge, once each
    member is realized and fused (``realize(strategy='manufacture')``),
    exports one 3MF with exactly 2 objects."""
    slug = "skillpin-2e"
    ops_lists = _extract_ops_lists(
        _skill_section(
            "precis-se-print-help",
            "2e — print groups: `intent='manufacture'` — the real part, print-in-place",
        )
    )
    assert len(ops_lists) == 4  # tree+intent, realize(knuckle), realize(pin), fuse
    handler.put(id=slug, text=json.dumps({"ops": ops_lists[0]}))
    handler.edit(id=slug, ops=ops_lists[1])
    handler.edit(id=slug, ops=ops_lists[2])
    _cut_bore(handler, slug)
    resp = handler.edit(id=slug, ops=ops_lists[3])
    assert "se_manufacture" in resp.body and "fused" in resp.body

    out = tmp_path / "hinge.3mf"
    handler.get(
        id=slug,
        view="print",
        args={"block": "hinge", "fmt": "3mf", "path": str(out)},
    )
    assert out.exists()
    objects = _3mf_objects(out)
    assert len(objects) == 2
