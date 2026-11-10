"""gr564575 — the repo-dev start page is a fixed-id, compact index.

``src/precis/data/skills/precis-repo-dev-start-help.md`` is the start
page a coding session loads once (local files carry the load line; the
same file in the checkout is the MCP-outage fallback). The slug is
load-bearing — a rename silently breaks the load line, the outage
fallback and any ``PRECIS_STARTUP_SKILLS`` pin — so it is pinned here.

The page stays an *index*: pointers to the memory layer, repo docs and
the canonical skill owners, never an inlined manual. A byte cap
ratchets that shape; the live graph handles it routes to are pinned as
text (the soul index me563907 + its selective bodies, the rulings hub,
the vocabulary root, the ``SPACE:repo-dev`` recall tag).

Pure file stats + ``startup_skills.resolve`` — no DB.
"""

from __future__ import annotations

from precis import startup_skills
from precis.utils.skill_common import parse_frontmatter
from tests._skill_roots import skill_path

SLUG = "precis-repo-dev-start-help"

#: Index, not manual: one trigger plus its owning page per line. Well
#: under ``test_skill_size.WARN_BYTES`` — a page that needs more than
#: this has started inlining what a linked skill owns.
START_PAGE_MAX_BYTES = 5000


def _text() -> str:
    return skill_path(SLUG).read_text(encoding="utf-8")


def test_start_page_exists_at_fixed_slug() -> None:
    """The slug is the contract — local files, the outage fallback and
    any pin name it verbatim."""
    assert skill_path(SLUG).is_file(), (
        f"{SLUG}.md missing — the repo-dev start page lives at a fixed "
        "slug; renaming it breaks the load line, the checkout fallback "
        "and every pin"
    )


def test_frontmatter_id_matches_the_fixed_slug() -> None:
    fm = parse_frontmatter(_text())
    assert fm.id == SLUG, (
        f"id: is {fm.id!r}, expected {SLUG!r} — the served slug comes "
        "from frontmatter and must match the file the checkout loads"
    )
    assert fm.status == "active"


def test_start_page_stays_an_index_not_a_manual() -> None:
    size = skill_path(SLUG).stat().st_size
    assert size <= START_PAGE_MAX_BYTES, (
        f"{SLUG} is {size} bytes (cap {START_PAGE_MAX_BYTES}) — the "
        "start page is a compact index; detail belongs to the skills "
        "and docs it points at"
    )


def test_start_page_resolves_for_pinning() -> None:
    """Delivery rides the cold-start pin path — the page must resolve
    through the same loader the banner uses."""
    result = startup_skills.resolve([SLUG], cap_kb=0)
    assert result.pinned == (SLUG,)
    assert not result.unknown


def test_start_page_carries_the_memory_layer_handles() -> None:
    """The soul index, rulings hub, vocabulary root and recall tag are
    the live entry points the page routes to — dropping one strands
    recall with no pointer."""
    text = _text()
    for anchor in ("me563907", "me489895", "tn559460", "SPACE:repo-dev"):
        assert anchor in text, f"{anchor} no longer appears in {SLUG}"


def test_start_page_names_the_checkout_as_authority() -> None:
    """The precedence rule keeps a stale served copy or memory from
    outranking the tree the session is editing — and names the same
    file as the MCP-outage fallback."""
    text = _text()
    for anchor in (
        "AGENTS.md",
        "docs/conventions/",
        "docs/codebase.md",
        "fallback",
    ):
        assert anchor in text, f"{anchor!r} no longer appears in {SLUG}"
