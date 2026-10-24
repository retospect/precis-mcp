"""Paper-text gate: no paper/journal chunk text in the public repo.

Ruling (Reto 2026-10-23, memory me509939; ``docs/conventions/invariants.md``):
a public git push is forever, so verbatim paper or journal text — the
``chunks`` table's upstream-copyrighted material — must never be committed.
Fixtures carry ids, sha256, numbers and short labels instead; when a test
needs the *shape* of paper text it builds it at runtime.

This walks ``tests/fixtures/`` and ``tests/data/`` — the two directories
where recorded prod data lands — and flags any single JSON/YAML/CSV string
value or any txt/md paragraph run over ~400 characters that reads as natural
language: mostly letters and spaces with several sentence-ending periods.
Ids, hashes, code and table rows stay silent (few spaces, few sentence
enders, a low letter fraction).

Files whose flagged prose IS paper/journal text are pinned in
``tests/data/paper_text_baseline.json`` as
``{relpath: [sha256-prefix per offending run]}``, a one-way floor:

* a NEW offending file fails — replace the prose with ids/hashes/numbers;
* a baselined file gaining or swapping a prose run fails (same fix, or a
  reviewed exemption below);
* a baselined file that is cleaned or reduced fails until its entry shrinks
  or goes — the baseline may only shrink.
  ``uv run python tests/test_paper_text_gate.py --write`` regenerates it and
  refuses to add or grow any entry.

``tests/fixtures/kinetics/tpd_reference_chunks.json`` was deliberately never
baselined: kin-tpd-fix deletes its prod-exported prose (2026-10-23); pinning
it would have grandfathered the file this ruling exists to remove.

Flagged prose that is NOT paper text — repo-authored fixture docs, golden
export output — takes an exemption, the ``secret-gate`` way
(``test_deploy_tree_no_secrets.py``): a ``paper-text-gate: allow — <reason>``
marker on a short comment line exempts the file; inside a long string it
exempts that one string. Where the format allows no comments or the bytes
are compared verbatim (JSON, golden files), the exception goes in
``tests/data/paper_text_allowlist.json`` — ``path -> reason``, one reason
per entry, a trailing ``/`` exempts a whole directory; an entry with no
offenders left fails as stale. A marker with no reason fails too.

An ``OSError`` during the walk or a read surfaces as ``ScanIncomplete``
(``tests/_policy_scan.py``) — never as a violation, never as a pass over a
partial tree.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

try:
    from tests._policy_scan import read_text, walk_files
except ModuleNotFoundError:
    # ``uv run python tests/test_paper_text_gate.py --write`` regenerates the
    # baseline; run as a script, sys.path[0] is tests/ so the package import
    # fails — put the repo root on the path and retry.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests._policy_scan import read_text, walk_files

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_ROOTS = (_REPO_ROOT / "tests" / "fixtures", _REPO_ROOT / "tests" / "data")
_BASELINE = Path(__file__).resolve().parent / "data" / "paper_text_baseline.json"
_ALLOWLIST = Path(__file__).resolve().parent / "data" / "paper_text_allowlist.json"

_SCAN_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".txt", ".md", ".csv"}

# "Looks like prose": a run over ~400 chars that is mostly letters and
# spaces and carries several sentence-ending periods. The three signals
# together keep ids/hashes (no spaces), tables and code (symbol-dense) and
# short labels silent.
_MIN_LEN = 400
_MIN_ALPHA_SPACE = 0.60
_MIN_SPACES = 20
_ENDER = re.compile(r"[.!?](?=\s|$)")
_MIN_ENDERS = 3

_ALLOW_MARKER = "paper-text-gate: allow"
# A marker on a short comment line exempts the whole file; a marker embedded
# in a prose run exempts only that run (the run's line is itself long).
_MARKER_LINE_MAX = 300
# A marker must carry `— <reason>` (any dash) of at least three words; a
# bare `paper-text-gate: allow` exempts nothing and fails the marker check.
_MARKER_REASON = re.compile(r"paper-text-gate:\s*allow\s*[—–-]{1,2}\s*(\S.*)$")
_MIN_REASON_WORDS = 3

_RULE = (
    "no paper/journal chunk text in the public repo "
    "(docs/conventions/invariants.md, Reto 2026-10-23) — fixtures carry ids, "
    "sha256 and numbers, not prose: replace the text, or mark a deliberate "
    f"exception '{_ALLOW_MARKER} — <reason>'"
)


def _is_prose(text: str) -> bool:
    t = text.strip()
    if len(t) <= _MIN_LEN:
        return False
    alpha_space = sum(c.isalpha() or c in " \t\n" for c in t)
    if alpha_space / len(t) < _MIN_ALPHA_SPACE:
        return False
    if t.count(" ") < _MIN_SPACES:
        return False
    return len(_ENDER.findall(t)) >= _MIN_ENDERS


def _strings(obj: object) -> Iterator[str]:
    """Every string scalar in a parsed JSON/YAML value, keys included."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _strings(value)


def _paragraphs(text: str) -> Iterator[str]:
    """Blank-line-separated runs — the unit for txt/md."""
    for block in re.split(r"\n\s*\n", text):
        if block.strip():
            yield block


def _units(path: Path, text: str) -> Iterator[str]:
    """Candidate units: parsed scalars for structured formats, paragraphs for
    txt/md. A file that fails to parse falls back to paragraph runs so a
    broken fixture can't dodge the scan."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            yield from _strings(json.loads(text))
        except json.JSONDecodeError:
            yield from _paragraphs(text)
    elif suffix == ".jsonl":
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                yield from _strings(json.loads(line))
            except json.JSONDecodeError:
                yield line
    elif suffix in (".yaml", ".yml"):
        try:
            yield from _strings(yaml.safe_load(text))
        except yaml.YAMLError:
            yield from _paragraphs(text)
    elif suffix == ".csv":
        try:
            fields = [f for row in csv.reader(io.StringIO(text)) for f in row]
        except csv.Error:
            fields = list(_paragraphs(text))
        yield from fields
    else:
        yield from _paragraphs(text)


def _marker_reason(text: str) -> str | None:
    """The marker's reason, or ``None`` when the marker is absent or has no
    reason — a bare ``paper-text-gate: allow`` exempts nothing."""
    m = _MARKER_REASON.search(text)
    if not m:
        return None
    reason = re.sub(r"\s*-+>\s*$", "", m.group(1)).strip()  # md comment tail
    return reason if len(reason.split()) >= _MIN_REASON_WORDS else None


def _file_marked(text: str) -> bool:
    """A reasoned marker on its own short (comment) line exempts the file."""
    return any(
        len(line.strip()) <= _MARKER_LINE_MAX and _marker_reason(line) is not None
        for line in text.splitlines()
    )


def _offenders(path: Path, text: str) -> list[str]:
    if _file_marked(text):
        return []
    return [
        unit
        for unit in _units(path, text)
        if _is_prose(unit) and _marker_reason(unit) is None
    ]


def _scan() -> dict[str, list[str]]:
    """``{relpath: [sha256-16 per offending run]}`` over the scanned dirs."""
    found: dict[str, list[str]] = {}
    for root in _SCAN_ROOTS:
        for path in walk_files(root):
            if path.suffix.lower() not in _SCAN_SUFFIXES:
                continue
            try:
                text = read_text(path)
            except UnicodeDecodeError:
                continue  # not text we can meaningfully scan
            hits = _offenders(path, text)
            if hits:
                found[path.relative_to(_REPO_ROOT).as_posix()] = sorted(
                    hashlib.sha256(u.encode("utf-8")).hexdigest()[:16] for u in hits
                )
    return found


def _load_table(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(read_text(path))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _baseline() -> dict[str, list[str]]:
    return _load_table(_BASELINE)


def _allowlist() -> dict[str, str]:
    return _load_table(_ALLOWLIST)


def _allowlisted(rel: str, allowlist: dict[str, str]) -> bool:
    """An entry names a file, or a directory when it ends in ``/``."""
    return rel in allowlist or any(
        entry.endswith("/") and rel.startswith(entry) for entry in allowlist
    )


def test_no_paper_prose_in_fixtures() -> None:
    """No NEW paper/journal prose run in tests/fixtures/ or tests/data/.

    A file with no baseline entry must stay clean; a baselined file must not
    gain runs the baseline does not pin."""
    found, baseline, allowlist = _scan(), _baseline(), _allowlist()
    problems: list[str] = []
    for rel, hashes in sorted(found.items()):
        if _allowlisted(rel, allowlist):
            continue
        new = sorted(set(hashes) - set(baseline.get(rel, [])))
        if new:
            kind = "new file" if rel not in baseline else "new prose in baselined file"
            problems.append(f"{kind}: {rel} (+{len(new)} prose run(s))")
    assert not problems, f"paper-text gate: {_RULE}.\n" + "\n".join(problems)


def test_paper_text_baseline_only_shrinks() -> None:
    """A baselined file that was cleaned fails until its entry shrinks or
    goes — the baseline records debt, not permission."""
    found = _scan()
    stale: list[str] = []
    for rel, hashes in sorted(_baseline().items()):
        if not (_REPO_ROOT / rel).is_file():
            stale.append(f"{rel}: file deleted — drop the entry")
            continue
        gone = sorted(set(hashes) - set(found.get(rel, [])))
        if gone:
            stale.append(
                f"{rel}: {len(gone)} of {len(hashes)} baselined run(s) no longer "
                "offend — shrink the entry"
            )
    assert not stale, (
        "paper-text baseline may only shrink — regenerate it with "
        "uv run python tests/test_paper_text_gate.py --write:\n" + "\n".join(stale)
    )


def test_paper_text_allowlist_entries_are_needed() -> None:
    """An allowlist entry covering no offenders is a stale exemption — the
    same reviewed-change discipline as the credential allowlist."""
    found = _scan()
    stale: list[str] = []
    for entry in sorted(_allowlist()):
        if entry.endswith("/"):
            covered = [r for r in found if r.startswith(entry)]
            if not (_REPO_ROOT / entry).is_dir() or not covered:
                stale.append(entry)
        elif not found.get(entry) or not (_REPO_ROOT / entry).is_file():
            stale.append(entry)
    assert not stale, (
        "allowlist entries with no prose run left — remove them from "
        f"{_ALLOWLIST.relative_to(_REPO_ROOT)}: {stale}"
    )


def test_no_malformed_paper_text_markers() -> None:
    """Every marker under the scanned dirs must carry ``— <reason>``."""
    bad: list[str] = []
    for root in _SCAN_ROOTS:
        for path in walk_files(root):
            if path.suffix.lower() not in _SCAN_SUFFIXES:
                continue
            try:
                text = read_text(path)
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _ALLOW_MARKER in line and _marker_reason(line) is None:
                    bad.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
    assert not bad, (
        f"marker(s) missing '— <reason>' ({_MIN_REASON_WORDS}+ words): {bad}"
    )


def test_fixture_walk_is_not_vacuous() -> None:
    """A silently empty walk would make the gate vacuously green."""
    scanned = [
        p
        for root in _SCAN_ROOTS
        for p in walk_files(root)
        if p.suffix.lower() in _SCAN_SUFFIXES
    ]
    assert len(scanned) > 50, f"fixture walk found only {len(scanned)} files"


def test_heuristic_flags_prose_not_ids_hashes_tables() -> None:
    """Self-check: a green gate on a clean tree must not mean a broken gate."""
    prose = (
        "The adsorption and desorption of the gas was studied across a range "
        "of surface temperatures. Each spectrum was recorded twice. The "
        "results agree with the earlier measurements within error bars. "
    ) * 3
    assert _is_prose(prose)
    quiet = [
        "sha256 " + "ab12cd34" * 30,  # ids and hashes: letters but no spaces
        " ".join(str(10 * i + j) for i in range(60) for j in range(8)),  # numbers
        "x = f(y); return z[3] + w.method()   " * 15,  # code: symbol-dense
    ]
    for sample in quiet:
        assert not _is_prose(sample), f"gate false-positived on: {sample[:60]}"


def test_marker_exempts_a_run_and_a_comment_line_exempts_the_file(
    tmp_path: Path,
) -> None:
    prose = (
        "Verbatim paper text goes here and keeps going for a long while. "
        "Sentence after sentence of natural language fills the field. "
        "A third sentence ends the paragraph with a period. "
    ) * 3
    path = tmp_path / "f.txt"
    path.write_text(prose, encoding="utf-8")
    assert len(_offenders(path, read_text(path))) == 1
    # a reasoned marker inside the run exempts that run only
    path.write_text(
        prose + f"({_ALLOW_MARKER} — planted self-test sample)\n", encoding="utf-8"
    )
    assert _offenders(path, read_text(path)) == []
    # a short standalone marker line exempts the file
    path.write_text(
        f"# {_ALLOW_MARKER} — synthetic docs fixture text\n\n{prose}",
        encoding="utf-8",
    )
    assert _offenders(path, read_text(path)) == []
    # a bare marker (no reason) exempts nothing
    path.write_text(f"# {_ALLOW_MARKER}\n\n{prose}", encoding="utf-8")
    assert len(_offenders(path, read_text(path))) == 1


def _write() -> int:
    """Regenerate the baseline — downwards only; refuses new offenders."""
    found = _scan()
    baseline = _baseline()
    allowlist = _allowlist()
    grew = sorted(
        rel
        for rel, hashes in found.items()
        if not _allowlisted(rel, allowlist)
        and (rel not in baseline or set(hashes) - set(baseline[rel]))
    )
    if grew:
        print(f"refusing: new offenders would grow the baseline: {grew}")
        return 1
    shrunk = {rel: found[rel] for rel in sorted(baseline) if rel in found}
    _BASELINE.write_text(
        json.dumps(shrunk, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    removed = sorted(set(baseline) - set(shrunk))
    print(f"wrote {_BASELINE.name}: {len(shrunk)} files (dropped: {removed or 'none'})")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        sys.exit(
            f"usage: {sys.argv[0]} --write  (regenerates the baseline downwards only)"
        )
    sys.exit(_write())
