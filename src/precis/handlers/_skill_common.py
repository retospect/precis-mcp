"""Shared helpers for the skill ingest path.

The frontmatter parser here replaces the hand-rolled YAML scanner at
``skill.py:_parse_frontmatter`` (predating the docs+skills redesign).
That older parser returned a flat ``dict[str, str]`` — fine when the
only fields were scalars (``status``, ``title``, ``applies-to``).
The redesign adds list fields (``invokes_personas:``) and adds
validation on ``flavor:``, so the parser graduates into a typed
:class:`SkillFrontmatter` dataclass.

See ``docs/design/docs-and-skills-redesign.md`` decision 7 (flavour
discriminator), decision 9 (runbook orchestration), decision 13
(availability gating).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: Defined flavours, per decision 7. The frontmatter field
#: ``flavor: <value>`` is emitted as a ``FLAVOR:<value>`` tag (uppercase
#: prefix → replaces within prefix, so a skill carries exactly one
#: flavour at a time).
VALID_FLAVORS: Final[tuple[str, ...]] = (
    "reference",
    "persona",
    "runbook",
    "concept",
)


class FrontmatterError(ValueError):
    """Raised on a hard-fail static gate (decision 7 / 9 / 13)."""


@dataclass(frozen=True)
class SkillFrontmatter:
    """Parsed frontmatter from a shipped skill markdown file.

    Field names are Python ``snake_case``; YAML keys may use the
    canonical kebab-case (``last-updated``, ``applies-to``,
    ``available-when``, ``invokes-personas``) or already-snake_case
    forms — both map to the same field.
    """

    #: Skill slug (``id:`` in YAML). Matches the filename stem in
    #: ``src/precis/data/skills/`` by convention.
    id: str | None = None

    #: Human-readable title for the skill.
    title: str | None = None

    #: Lifecycle status (e.g. ``active``, ``draft``, ``phase-10``).
    status: str | None = None

    #: Cold-start budgeting tier (legacy; carried for compatibility
    #: with existing files until the migration sweeps them).
    tier: str | None = None

    #: Cold-start budgeting floor (legacy; same as ``tier``).
    floor: str | None = None

    #: Free-text scope statement, e.g. ``"put (every kind that
    #: supports it)"``. Used by the availability gate to derive the
    #: kind(s) the skill applies to.
    applies_to: str | None = None

    #: ISO-ish date the skill was last edited. Authored manually.
    last_updated: str | None = None

    #: Flavour discriminator, one of :data:`VALID_FLAVORS`. ``None``
    #: when the skill predates the redesign and hasn't been migrated.
    flavor: str | None = None

    #: For ``flavor='runbook'`` skills: slugs of persona skills that
    #: this runbook orchestrates. Each entry must resolve to an
    #: existing persona file at ingest time (validated downstream,
    #: not here — this parser is pure text).
    invokes_personas: tuple[str, ...] = ()

    #: Env-var name gating availability (e.g. ``"PRECIS_EPO_KEY"``).
    #: Mirrored at ingest as a tag the search-time filter respects.
    available_when: str | None = None

    #: Any frontmatter keys we don't model explicitly. Preserved so a
    #: skill can carry experimental metadata without the parser
    #: rejecting it; the ingest pipeline can decide what to do with
    #: each entry.
    extra: dict[str, str] = field(default_factory=dict)


# Map canonical kebab-case YAML keys → dataclass field names.
# Forward map (kebab → snake) and reverse identity (snake passes through).
_KEY_ALIASES: Final[dict[str, str]] = {
    "applies-to": "applies_to",
    "last-updated": "last_updated",
    "available-when": "available_when",
    "invokes-personas": "invokes_personas",
}

# The fields a SkillFrontmatter actually carries (excluding ``extra``).
# Computed once at import time; used to route parsed key/value pairs
# into known fields vs the ``extra`` bucket.
_KNOWN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id", "title", "status", "tier", "floor",
        "applies_to", "last_updated", "flavor",
        "invokes_personas", "available_when",
    }
)


def parse_frontmatter(text: str) -> SkillFrontmatter:
    """Parse YAML-ish frontmatter from a skill markdown file.

    Supports scalar ``key: value`` pairs and two list shapes for
    ``invokes-personas:`` —

    Block:
    ```
    invokes-personas:
      - precis-adversarial-reviewer
      - precis-citation-reviewer
    ```

    Or inline comma-separated:
    ```
    invokes-personas: precis-adversarial-reviewer, precis-citation-reviewer
    ```

    Raises :class:`FrontmatterError` when ``flavor:`` is set to a
    value outside :data:`VALID_FLAVORS` (the only validation that
    happens here; cross-skill resolution like
    ``invokes_personas`` membership is downstream).

    Files without leading ``---`` frontmatter return an empty
    :class:`SkillFrontmatter`.
    """
    if not text.startswith("---"):
        return SkillFrontmatter()

    end = text.find("\n---", 3)
    if end == -1:
        return SkillFrontmatter()

    body = text[3:end].lstrip("\n")
    raw: dict[str, object] = {}

    # State for capturing a YAML block list: when a key appears with
    # an empty value, subsequent indented ``- item`` lines feed it.
    current_list_key: str | None = None
    current_list: list[str] = []

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            # Blank line ends any list-in-progress (no further items).
            if current_list_key is not None:
                raw[current_list_key] = tuple(current_list)
                current_list_key = None
                current_list = []
            continue

        # Indented ``- item`` → append to the list-in-progress.
        if current_list_key is not None and line.lstrip().startswith("- "):
            item = line.lstrip()[2:].strip().strip("\"'")
            if item:
                current_list.append(item)
            continue

        # Otherwise this line is a key (closes any list-in-progress).
        if current_list_key is not None:
            raw[current_list_key] = tuple(current_list)
            current_list_key = None
            current_list = []

        if ":" not in line:
            continue

        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()

        if not val:
            # Empty value → next indented lines are list items.
            current_list_key = key
            continue

        # Inline comma-separated list, only for keys we know take lists.
        list_keys = {"invokes-personas", "invokes_personas"}
        if key in list_keys and "," in val:
            items = [v.strip().strip("\"'") for v in val.split(",")]
            raw[key] = tuple(v for v in items if v)
        else:
            raw[key] = val.strip("\"'")

    # Flush any trailing list.
    if current_list_key is not None:
        raw[current_list_key] = tuple(current_list)

    # Map kebab → snake, sort into known vs extra.
    fields_in: dict[str, object] = {}
    extra: dict[str, str] = {}
    for key, val in raw.items():
        canon = _KEY_ALIASES.get(key, key.replace("-", "_"))
        if canon in _KNOWN_FIELDS:
            fields_in[canon] = val
        else:
            # Only scalars land in extra; lists for unknown keys are
            # too unusual to model generically. Coerce to string.
            extra[key] = str(val) if not isinstance(val, tuple) else ",".join(val)

    # Validate flavor early — hard-fail per decision 7.
    flavor_val = fields_in.get("flavor")
    if flavor_val is not None and flavor_val not in VALID_FLAVORS:
        raise FrontmatterError(
            f"flavor={flavor_val!r} is not one of {VALID_FLAVORS}"
        )

    # invokes_personas: always tuple[str, ...] regardless of input shape.
    ip = fields_in.get("invokes_personas")
    if ip is None:
        fields_in["invokes_personas"] = ()
    elif isinstance(ip, str):
        # A bare scalar (no commas, no block) — single-item list.
        fields_in["invokes_personas"] = (ip,) if ip else ()
    # else already tuple from the parser

    return SkillFrontmatter(extra=extra, **fields_in)  # type: ignore[arg-type]


def flavor_tag(fm: SkillFrontmatter) -> str | None:
    """Return the ``FLAVOR:<value>`` tag string for a frontmatter,
    or ``None`` if no flavour is declared.

    Centralised so the ingest path can't drift from the convention.
    Decision 7: uppercase prefix → replaces within prefix.
    """
    if fm.flavor is None:
        return None
    return f"FLAVOR:{fm.flavor}"
