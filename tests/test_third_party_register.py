"""Third-party register gate — every vendored/adapted piece is registered.

The repo is public: a copied-in file carries licence obligations that must
be auditable in one place. ``docs/reference/third-party.md`` is that place —
one row per vendored or adapted piece naming source, licence, where it
landed and where the notice lives (gr608720).

This test scans ``src/`` (skills included) for the provenance idioms
``adapted from`` / ``vendored from`` / ``ported from`` — word-bounded and
case-insensitive, so ``imported from`` / ``exported from`` /
``reported from`` do not match. A hit must be either:

* **covered** — a register row's *Landed* entry names the file, a
  directory above it (``path/``), ``path::Symbol``, or ``skill:<slug>``;
  or
* **exempt** — the line or the one above it carries
  ``third-party: n/a — <reason>``, for idioms that are not vendoring
  claims (the figure-caption pointer vocabulary in
  ``taproot/backfill.py``, same-repo ports like the retired classic
  ``/drafts`` reader, a ported *ruling*).

Each row's *Licence* is gated to the allowlist (Reto 2026-11-11): MIT,
BSD(-N-Clause), Apache-2.0, LGPL when dynamically linked, and
``first-party`` — our own code crossing a repo boundary carries no
third-party licence obligation.

An ``OSError`` mid-walk surfaces as ``ScanIncomplete``
(``tests/_policy_scan.py``), never as a verdict.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from tests._policy_scan import read_text, walk_files

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
_REGISTER = _REPO_ROOT / "docs" / "reference" / "third-party.md"

_MARKER = re.compile(r"\b(?:adapted|vendored|ported)\s+from\b", re.IGNORECASE)
_NA = "third-party: n/a"
_SKIP_DIRS = {"__pycache__"}

# Reto 2026-11-11 (gr608720): the licences vendored or adapted code may
# carry in this tree. ``first-party`` is our own code arriving from a
# sibling repo — no third-party obligation; the upstream licence is noted
# in the cell's parenthetical where it matters.
_LICENCE_ALLOW = re.compile(
    r"MIT|BSD(?:-\d-Clause)?|Apache-2\.0|LGPL\S* \(dynamic\)|first-party\b.*"
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class _Row(NamedTuple):
    piece: str
    source: str
    licence: str
    landed: list[str]  # repo-relative paths resolved from the Landed cell
    date: str
    notice: str


def _resolve_landed(cell: str) -> list[str]:
    """Landed cell → repo-relative paths. Entries are separated by ``<br>``;
    ``skill:<slug>`` maps to ``src/precis/data/skills/<slug>.md`` and
    ``path::Symbol`` resolves to its path."""
    out: list[str] = []
    for part in re.split(r"<br\s*/?>", cell):
        entry = part.strip().strip("`").strip()
        if not entry:
            continue
        if entry.startswith("skill:"):
            entry = f"src/precis/data/skills/{entry.removeprefix('skill:')}.md"
        else:
            entry = entry.split("::", 1)[0].rstrip("/")
        out.append(entry)
    return out


def _register_rows(path: Path = _REGISTER) -> list[_Row]:
    """Parsed rows of the register's markdown table (header + rule skipped)."""
    rows: list[_Row] = []
    for line in read_text(path).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] == "Piece" or set(cells[0]) <= {"-", ":"}:
            continue
        rows.append(
            _Row(
                piece=cells[0],
                source=cells[1],
                licence=cells[2],
                landed=_resolve_landed(cells[3]),
                date=cells[4],
                notice=cells[5],
            )
        )
    return rows


def _marker_hits(root: Path) -> list[tuple[str, int, str, bool]]:
    """``(relpath, lineno, line, exempt)`` per provenance idiom under ``root``.

    ``relpath`` is repo-relative when the file sits under the checkout,
    root-relative otherwise (unit-test trees). The ``third-party: n/a``
    marker exempts its own line or the line right after it — same rule as
    the secret gate's ``allow`` marker, so a reflowed comment can't strand
    the idiom on a bare line.
    """
    hits: list[tuple[str, int, str, bool]] = []
    for path in walk_files(root, skip_dirs=_SKIP_DIRS):
        base = _REPO_ROOT if path.is_relative_to(_REPO_ROOT) else root
        rel = path.relative_to(base).as_posix()
        try:
            text = read_text(path)
        except UnicodeDecodeError:
            continue  # not text we can meaningfully scan
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _MARKER.search(line):
                exempt = _NA in line or (lineno >= 2 and _NA in lines[lineno - 2])
                hits.append((rel, lineno, line.strip(), exempt))
    return hits


def _covered(rel: str, rows: list[_Row]) -> bool:
    """A row covers ``rel`` when a landed path names it or a dir above it."""
    return any(
        rel == landed or rel.startswith(landed + "/")
        for row in rows
        for landed in row.landed
    )


def test_every_provenance_idiom_is_registered() -> None:
    rows = _register_rows()
    hits = _marker_hits(_SRC)
    assert hits, "no provenance idioms found under src/ — the walk went vacuous"
    bad = [
        f"{rel}:{lineno}: {line[:100]}"
        for rel, lineno, line, exempt in hits
        if not exempt and not _covered(rel, rows)
    ]
    assert not bad, (
        "provenance idiom(s) with no register row — add a row to "
        "docs/reference/third-party.md whose Landed cell names the file "
        "(or a dir above it, path::Symbol, or skill:<slug>), or mark a "
        "non-vendoring use with 'third-party: n/a — <reason>':\n" + "\n".join(bad)
    )


def test_register_licences_are_allowlisted() -> None:
    bad = [
        f"{row.piece}: {row.licence!r}"
        for row in _register_rows()
        if not _LICENCE_ALLOW.fullmatch(row.licence)
    ]
    assert not bad, (
        "register row(s) with a licence outside the allowlist "
        "(MIT, BSD(-N-Clause), Apache-2.0, LGPL-* (dynamic), first-party) — "
        "a new licence is a policy decision (gr608720):\n" + "\n".join(bad)
    )


def test_register_landed_paths_exist() -> None:
    bad = [
        f"{row.piece}: {landed}"
        for row in _register_rows()
        for landed in row.landed
        if not (_REPO_ROOT / landed).exists()
    ]
    assert not bad, (
        "register row(s) pointing at paths that no longer exist — "
        "update or drop the row:\n" + "\n".join(bad)
    )


def test_register_rows_are_well_formed() -> None:
    rows = _register_rows()
    assert rows, f"no table rows parsed from {_REGISTER.relative_to(_REPO_ROOT)}"
    bad = [
        row.piece or "(empty piece)"
        for row in rows
        if not all((row.piece, row.source, row.licence, row.notice))
        or not row.landed
        or not _DATE.fullmatch(row.date)
    ]
    assert not bad, (
        "register row(s) with an empty cell or a non-ISO date "
        "(want Piece | Source | Licence | Landed | YYYY-MM-DD | Notice):\n"
        + "\n".join(bad)
    )


def test_marker_regex_boundaries() -> None:
    """Self-check: the idiom regex catches the real shapes and not the
    ``-ported`` lookalikes (``imported``/``exported``/``reported``)."""
    for s in ("Adapted from x", "vendored from y", "Ported from z", "ADAPTED FROM w"):
        assert _MARKER.search(s), f"missed idiom: {s!r}"
    for s in (
        "imported from x",
        "exported from y",
        "reported from z",
        "re-exported from q",
    ):
        assert not _MARKER.search(s), f"false positive: {s!r}"


def test_na_marker_exempts_its_line_and_the_next(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("# vendored from elsewhere\n", encoding="utf-8")
    assert _marker_hits(tmp_path)[0][3] is False
    f.write_text(
        "# third-party: n/a — a ruling, not code\n# ported from band space\n",
        encoding="utf-8",
    )
    assert _marker_hits(tmp_path)[0][3] is True
    f.write_text(
        "# ported from band space  # third-party: n/a — ruling\n", encoding="utf-8"
    )
    assert _marker_hits(tmp_path)[0][3] is True
