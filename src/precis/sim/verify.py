"""``precis sim verify`` — lit-search-verify a sim's low-confidence YAML entries.

Slice 1 of ``docs/proposals/sim-harness.md`` (In-scope item 4, AC #4/#5,
the "Verify judge trust — DECIDED" entry). For each ``verify:`` YAML entry
flagged ``verified: false`` (or a ``confidence`` below a floor):

1. **lit-search** precis read-only for supporting corpus chunks;
2. an **LLM judge** returns ``{value_ok, citation_ref, note}`` — biased hard
   toward *unverified* (a dispatch error, unparseable output, or a
   ``citation_ref`` that doesn't resolve to one of the search hits all degrade
   to ``value_ok=False``, never a silent flip);
3. for each entry the judge clears, **write back** ``verified: true`` +
   ``source:`` into the repo YAML and **git-commit** the delta on a
   ``precis-verify/<date>`` branch (never the sim's default branch — review is
   the merge), **mint** a ``material`` entity + a ``citation`` in precis, and
   **append** a ``milestone`` deed to the registry-linked quest's logbook.

The core :func:`verify_sim` is pure orchestration over two **injected**
callables — a :data:`SearchFn` and a :data:`JudgeFn` — so the gate exercises
it fully offline (no network, no git, no precis writes) with fakes.
:func:`make_corpus_search_fn` / :func:`make_llm_judge_fn` are the real
implementations the CLI wires. ``--dry-run`` runs steps 1-2 and renders the
exact YAML diff it *would* commit, then stops before any write (step 3).

The YAML writeback is a **targeted text edit** keyed by each entry's ``id:``,
not a ``yaml.dump`` round-trip — it preserves the file's comments and yields a
minimal, reviewable per-entry diff.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from precis.sim.manifest import SimManifest
from precis.sim.registry import SimEntry

if TYPE_CHECKING:
    from precis.dispatch import Hub
    from precis.store import Store

log = logging.getLogger(__name__)

#: An entry carrying a ``confidence`` at or below this floor is flagged for
#: verification even when it isn't ``verified: false`` outright.
DEFAULT_CONFIDENCE_FLOOR = 0.8


# ── the value types (all frozen — the orchestration is pure) ───────────────


@dataclass(frozen=True, slots=True)
class FlaggedEntry:
    """One YAML entry that needs verifying."""

    entry_id: str
    name: str
    yaml_file: Path
    """Absolute path of the ``verify:`` file this entry came from."""
    rel_file: str
    """``yaml_file`` relative to the sim repo root (for messages + diff labels)."""
    data: dict[str, Any]
    reason: str
    """Why it was flagged — ``"verified:false"`` or ``"confidence<floor"``."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One read-only corpus chunk the lit-search surfaced."""

    handle: str
    """The chunk's stable citation handle (``Block.slug``, e.g. ``collins06~7``)."""
    quote: str
    ref_slug: str
    """Slug of the source ref (e.g. the paper), for ``link='paper:<slug>'``."""
    source_kind: str
    score: float


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """The LLM judge's per-entry answer (AC #4 record fields)."""

    value_ok: bool
    citation_ref: str | None
    note: str


@dataclass(frozen=True, slots=True)
class VerifyRecord:
    """One entry's outcome — the ``{entry, value_ok, citation_ref, note}`` AC #4
    record, plus the resolved citation quote/ref and the flip decision."""

    entry: str
    name: str
    rel_file: str
    value_ok: bool
    citation_ref: str | None
    note: str
    yaml_file: Path
    citation_quote: str = ""
    citation_ref_slug: str = ""
    will_flip: bool = False
    """``True`` iff the judge cleared it *and* the citation resolved to a real
    hit — the only case a writeback/mint/flip happens for."""


@dataclass(frozen=True, slots=True)
class FileDiff:
    """The rendered writeback for one YAML file."""

    rel_file: str
    diff: str
    new_text: str


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """Everything one ``precis sim verify`` run produced."""

    records: tuple[VerifyRecord, ...]
    diffs: tuple[FileDiff, ...]
    flagged: int
    verified: int
    applied: bool
    branch: str | None = None
    messages: tuple[str, ...] = field(default_factory=tuple)


#: A read-only lit-search: query text -> ranked corpus hits.
SearchFn = Callable[[str], list[SearchHit]]
#: The judge: (flagged entry, its search hits) -> a verdict.
JudgeFn = Callable[[FlaggedEntry, list[SearchHit]], JudgeVerdict]


# ── scan — find the entries that need verifying ────────────────────────────


def _iter_entry_dicts(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every entry-mapping (a dict with a string ``id``) reachable in
    *node*, without descending into an entry's own values.

    Handles both the ``materials: [ {id: ...}, ... ]`` list-of-records shape
    (``lighterthanair``) and a flat top-level list; a dict with no ``id`` is
    a container we recurse through.
    """
    if isinstance(node, dict):
        if isinstance(node.get("id"), str):
            yield node
            return
        for value in node.values():
            yield from _iter_entry_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_entry_dicts(item)


def _flag_reason(entry: dict[str, Any], *, floor: float) -> str | None:
    """Why *entry* needs verifying, or ``None`` if it doesn't.

    An entry is flagged when it declares ``verified`` and the value is falsy,
    or when it carries a numeric ``confidence`` below *floor*. An entry with
    **no** ``verified``/``confidence`` scheme (e.g. a plain catalog) is left
    alone — it opts in by adding one, per the proposal's motivation.
    """
    if "verified" in entry and not entry["verified"]:
        return "verified:false"
    conf = entry.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf < floor:
        return f"confidence<{floor}"
    return None


def scan_entries(
    entry: SimEntry, manifest: SimManifest, *, floor: float = DEFAULT_CONFIDENCE_FLOOR
) -> list[FlaggedEntry]:
    """Load every ``manifest.verify`` YAML under ``entry.path`` and collect the
    flagged entries, in file-then-document order.

    A ``verify:`` path that doesn't exist or doesn't parse is skipped (logged),
    not fatal — one malformed file shouldn't sink the whole run.
    """
    flagged: list[FlaggedEntry] = []
    for rel in manifest.verify:
        path = (entry.path / rel).resolve()
        if not path.is_file():
            log.warning("sim verify: verify file not found, skipping: %s", path)
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # pragma: no cover - defensive
            log.warning("sim verify: could not parse %s: %s", path, exc)
            continue
        for record in _iter_entry_dicts(raw):
            reason = _flag_reason(record, floor=floor)
            if reason is None:
                continue
            entry_id = str(record["id"])
            flagged.append(
                FlaggedEntry(
                    entry_id=entry_id,
                    name=str(record.get("name") or entry_id),
                    yaml_file=path,
                    rel_file=rel,
                    data=record,
                    reason=reason,
                )
            )
    return flagged


def build_query(flagged: FlaggedEntry) -> str:
    """A lit-search query for *flagged* — its name/class plus a few of its own
    property keys, so the search anchors on the material and its quantities."""
    data = flagged.data
    parts: list[str] = [str(data.get("name") or flagged.entry_id)]
    for key in ("material_class", "tier"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    numeric_keys = [
        key
        for key, val in data.items()
        if isinstance(val, (int, float)) and not isinstance(val, bool)
    ]
    parts.extend(numeric_keys[:4])
    return " ".join(parts)


# ── plan — search + judge each flagged entry (pure over injected fns) ───────


def plan_verify(
    flagged: list[FlaggedEntry], *, search_fn: SearchFn, judge_fn: JudgeFn
) -> list[VerifyRecord]:
    """Run search + judge for each flagged entry and resolve the chosen
    citation back to a real hit (quote + source ref).

    ``will_flip`` is set only when the judge said ``value_ok`` **and** its
    ``citation_ref`` matches one of the hits (so a real quote/source exists) —
    a hallucinated handle degrades to no flip.
    """
    records: list[VerifyRecord] = []
    for fe in flagged:
        hits = search_fn(build_query(fe))
        verdict = judge_fn(fe, hits)
        quote = ""
        ref_slug = ""
        if verdict.citation_ref:
            for hit in hits:
                if hit.handle == verdict.citation_ref:
                    quote = hit.quote
                    ref_slug = hit.ref_slug
                    break
        will_flip = bool(verdict.value_ok and verdict.citation_ref and quote)
        records.append(
            VerifyRecord(
                entry=fe.entry_id,
                name=fe.name,
                rel_file=fe.rel_file,
                value_ok=verdict.value_ok,
                citation_ref=verdict.citation_ref,
                note=verdict.note,
                yaml_file=fe.yaml_file,
                citation_quote=quote,
                citation_ref_slug=ref_slug,
                will_flip=will_flip,
            )
        )
    return records


# ── writeback rendering — targeted, comment-preserving YAML text edit ───────

_LIST_ID_RE = r"""^(?P<indent>\s*)-\s+id:\s*['"]?{id}['"]?\s*(#.*)?$"""


def _entry_block_range(lines: list[str], entry_id: str) -> tuple[int, int] | None:
    """Locate the ``- id: <entry_id>`` list item's line range ``[start, end)``.

    ``start`` is the ``- id:`` line; ``end`` is the first later line at or below
    the dash's indent (the next item / a dedent), or EOF. Returns ``None`` if
    the entry isn't found in list-item form.
    """
    id_re = re.compile(_LIST_ID_RE.format(id=re.escape(entry_id)))
    start = None
    dash_indent = 0
    for i, line in enumerate(lines):
        m = id_re.match(line)
        if m is not None:
            start = i
            dash_indent = len(m.group("indent"))
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped:
            continue
        indent = len(lines[j]) - len(lines[j].lstrip())
        if indent <= dash_indent:
            end = j
            break
    return start, end


def _source_value(existing_line: str | None, citation_ref: str) -> str:
    """The new ``source:`` flow-list value — the original hint (if any) plus
    the citation handle, JSON-encoded (valid YAML for simple scalars)."""
    items: list[str] = []
    if existing_line is not None:
        raw = existing_line.split(":", 1)[1].strip()
        raw = re.sub(r"\s+#.*$", "", raw).strip()
        if raw and not raw.startswith("["):
            items.append(raw.strip("'\""))
    if citation_ref not in items:
        items.append(citation_ref)
    return json.dumps(items)


def _flip_entry_text(text: str, entry_id: str, citation_ref: str) -> str:
    """Return *text* with *entry_id*'s block flipped to ``verified: true`` and
    its ``source:`` set to include *citation_ref* — a no-op if not found."""
    lines = text.splitlines(keepends=True)
    span = _entry_block_range(lines, entry_id)
    if span is None:
        log.warning("sim verify: entry %r not found for writeback", entry_id)
        return text
    start, end = span
    verified_re = re.compile(r"^(?P<indent>\s*)verified:\s*\S.*$")
    source_re = re.compile(r"^(?P<indent>\s*)source:\s*.*$")
    verified_idx = None
    source_idx = None
    field_indent = None
    for i in range(start + 1, end):
        if field_indent is None and lines[i].strip():
            field_indent = len(lines[i]) - len(lines[i].lstrip())
        if verified_idx is None and verified_re.match(lines[i]):
            verified_idx = i
        if source_idx is None and source_re.match(lines[i]):
            source_idx = i
    if verified_idx is not None:
        m = verified_re.match(lines[verified_idx])
        assert m is not None
        eol = "\n" if lines[verified_idx].endswith("\n") else ""
        lines[verified_idx] = f"{m.group('indent')}verified: true{eol}"
    src_line = lines[source_idx] if source_idx is not None else None
    new_source_value = _source_value(src_line, citation_ref)
    indent = " " * (field_indent if field_indent is not None else 4)
    if source_idx is not None:
        m2 = source_re.match(lines[source_idx])
        assert m2 is not None
        eol = "\n" if lines[source_idx].endswith("\n") else ""
        lines[source_idx] = f"{m2.group('indent')}source: {new_source_value}{eol}"
    else:
        insert_at = verified_idx if verified_idx is not None else end - 1
        lines.insert(insert_at, f"{indent}source: {new_source_value}\n")
    return "".join(lines)


def render_writebacks(records: list[VerifyRecord]) -> list[FileDiff]:
    """Group the flip-worthy records by file, apply all flips per file, and
    return the minimal unified diff + new text for each touched file."""
    by_file: dict[Path, list[VerifyRecord]] = {}
    for rec in records:
        if rec.will_flip and rec.citation_ref is not None:
            by_file.setdefault(rec.yaml_file, []).append(rec)
    diffs: list[FileDiff] = []
    for path, recs in sorted(by_file.items()):
        original = path.read_text(encoding="utf-8")
        new = original
        for rec in recs:
            assert rec.citation_ref is not None
            new = _flip_entry_text(new, rec.entry, rec.citation_ref)
        rel = recs[0].rel_file
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        diffs.append(FileDiff(rel_file=rel, diff=diff, new_text=new))
    return diffs


# ── the orchestrator ───────────────────────────────────────────────────────


def verify_sim(
    *,
    slug: str,
    entry: SimEntry,
    manifest: SimManifest,
    search_fn: SearchFn,
    judge_fn: JudgeFn,
    dry_run: bool,
    store: Store | None = None,
    hub: Hub | None = None,
    floor: float = DEFAULT_CONFIDENCE_FLOOR,
    today: _dt.date | None = None,
) -> VerifyOutcome:
    """Verify one sim's flagged YAML entries.

    Reads only (search + judge) when ``dry_run`` — the records and the exact
    YAML diff are produced but no file, git, or precis write happens. Otherwise
    the flips are written to disk, git-committed on a ``precis-verify/<date>``
    branch, and each flipped entry mints a ``material`` + ``citation`` and
    appends a quest deed (``store`` + ``hub`` are then required).
    """
    flagged = scan_entries(entry, manifest, floor=floor)
    records = plan_verify(flagged, search_fn=search_fn, judge_fn=judge_fn)
    diffs = render_writebacks(records)
    n_verified = sum(1 for r in records if r.will_flip)
    messages: list[str] = []

    if dry_run:
        messages.append("dry-run: no files written, no git, no precis writes")
        return VerifyOutcome(
            records=tuple(records),
            diffs=tuple(diffs),
            flagged=len(flagged),
            verified=n_verified,
            applied=False,
            branch=None,
            messages=tuple(messages),
        )

    if store is None or hub is None:
        raise ValueError("verify_sim: a live run requires store= and hub=")

    branch: str | None = None
    if diffs:
        for file_diff in diffs:
            (entry.path / file_diff.rel_file).write_text(
                file_diff.new_text, encoding="utf-8"
            )
        day = today or _dt.date.today()
        branch = f"precis-verify/{day.isoformat()}"
        committed = _git_commit(
            entry.path,
            [d.rel_file for d in diffs],
            branch=branch,
            message=f"verify: {slug} — {n_verified} entries verified via precis",
        )
        if not committed:
            branch = None
            messages.append("git: nothing to commit (files unchanged?)")

    for rec in records:
        if rec.will_flip:
            _mint_material_and_citation(hub, store, rec, sim_slug=slug)

    deed_msg = _append_quest_deed(
        store,
        quest=entry.quest,
        sim_slug=slug,
        flagged=len(flagged),
        verified=n_verified,
        branch=branch,
    )
    if deed_msg:
        messages.append(deed_msg)

    return VerifyOutcome(
        records=tuple(records),
        diffs=tuple(diffs),
        flagged=len(flagged),
        verified=n_verified,
        applied=True,
        branch=branch,
        messages=tuple(messages),
    )


# ── write-side helpers (only reached on a non-dry run) ──────────────────────


def _git_commit(repo: Path, rel_files: list[str], *, branch: str, message: str) -> bool:
    """Commit *rel_files* in *repo* on *branch* (created if absent). Returns
    ``True`` if a commit was made, ``False`` if there was nothing staged.

    Never the sim's default branch — the flips land on ``precis-verify/<date>``
    for a human to review and merge (the "Verify judge trust" decision).
    """

    def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=30,
        )

    exists = (
        _git("rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0
    )
    _git("checkout", branch) if exists else _git("checkout", "-b", branch)
    _git("add", *rel_files)
    # Nothing staged (e.g. the flip re-ran and matched) -> no empty commit.
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git("commit", "-m", message)
    return True


def _material_slug(entry_id: str) -> str:
    """A material ref slug from a sim entry id — lowercase, ``[a-z0-9-]``."""
    slug = re.sub(r"[^a-z0-9]+", "-", entry_id.lower()).strip("-")
    return slug or entry_id.lower()


def _mint_material_and_citation(
    hub: Hub, store: Store, rec: VerifyRecord, *, sim_slug: str
) -> None:
    """Upsert a ``material`` entity for a verified entry and mint the
    ``citation`` that grounds it — best-effort, logged not raised on failure."""
    from precis.errors import BadInput, NotFound
    from precis.handlers.citation import CitationHandler
    from precis.handlers.material import MaterialHandler

    slug = _material_slug(rec.entry)
    try:
        MaterialHandler(hub=hub).put(
            id=slug,
            title=rec.name,
            meta={"aliases": [rec.entry], "notes": f"verified via sim {sim_slug}"},
        )
    except (BadInput, NotFound) as exc:  # pragma: no cover - defensive
        log.warning("sim verify: material mint failed for %s: %s", slug, exc)
        return
    if not rec.citation_ref or not rec.citation_quote:
        return
    link = f"paper:{rec.citation_ref_slug}" if rec.citation_ref_slug else None
    try:
        CitationHandler(hub=hub).put(
            text=f"{rec.name}: material properties verified for sim {sim_slug}",
            source_handle=rec.citation_ref,
            source_quote=rec.citation_quote,
            verifier_confidence=1.0,
            link=link,
            rel="cites" if link else None,
        )
    except (BadInput, NotFound) as exc:  # pragma: no cover - defensive
        log.warning("sim verify: citation mint failed for %s: %s", slug, exc)


def _append_quest_deed(
    store: Store,
    *,
    quest: str | None,
    sim_slug: str,
    flagged: int,
    verified: int,
    branch: str | None,
) -> str | None:
    """Append a ``milestone`` deed to the registry-linked quest's logbook.

    ``by='system'`` — this is a measured verify result, not model narration
    (:data:`precis.quest.logbook.MEASURED_BY`). Returns a human message when
    the quest can't be resolved (skipped, not fatal) or ``None`` on success.
    """
    if not quest:
        return "quest deed: skipped (no quest linked in registry)"
    from precis.quest.logbook import MEASURED_BY, append_entry

    quest_id: int | str = int(quest) if str(quest).isdigit() else quest
    quest_ref = store.get_ref(kind="quest", id=quest_id)
    if quest_ref is None:
        return f"quest deed: skipped (quest {quest!r} not found)"
    branch_note = f" (committed on {branch})" if branch else ""
    text = (
        f"sim verify {sim_slug}: {verified}/{flagged} flagged entries "
        f"verified against the corpus{branch_note}."
    )
    append_entry(store, quest_ref.id, text=text, entry_type="milestone", by=MEASURED_BY)
    return None


# ── the real search + judge the CLI wires (fakes replace these in the gate) ─


def make_corpus_search_fn(
    store: Store, *, kinds: list[str] | None = None, limit: int = 8
) -> SearchFn:
    """A read-only lit-search over the corpus, RRF-fused, one best chunk per
    ref. Kinds with no embedded chunks contribute nothing, so an over-broad
    set is harmless."""
    search_kinds = kinds or ["paper", "datasheet", "markdown", "plaintext", "material"]

    def _search(query: str) -> list[SearchHit]:
        try:
            rows = store.search_chunks_across_kinds(
                kinds=search_kinds, q=query, limit=limit
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("sim verify: corpus search failed for %r: %s", query, exc)
            return []
        hits: list[SearchHit] = []
        for block, ref, score in rows:
            if not block.slug:
                continue
            hits.append(
                SearchHit(
                    handle=block.slug,
                    quote=(block.text or "").strip()[:400],
                    ref_slug=ref.slug or "",
                    source_kind=ref.kind,
                    score=float(score),
                )
            )
        return hits

    return _search


_JUDGE_SYS = (
    "You are a careful materials-data verifier. You are given a material data "
    "entry and a set of corpus excerpts. Reply with ONLY the requested JSON "
    "object, no prose."
)

_JUDGE_PROMPT = """\
A simulation declares this material data entry, currently UNVERIFIED:

{entry_json}

Here are corpus excerpts that may support or refute its values, each with a
citation handle:

{hits_block}

Decide whether the entry's declared values are consistent with the evidence.
Only say value_ok=true if at least one excerpt genuinely supports the entry,
and set citation_ref to THAT excerpt's handle (verbatim, exactly as shown).
If nothing supports it, value_ok=false and citation_ref=null.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "value_ok": true | false,
  "citation_ref": "<handle from the list, or null>",
  "note": "<one sentence>"
}}
"""


def _render_judge_prompt(flagged: FlaggedEntry, hits: list[SearchHit]) -> str:
    entry_json = json.dumps(flagged.data, indent=2, sort_keys=True, default=str)
    hits_block = "\n\n".join(f"[{h.handle}] ({h.source_kind}) {h.quote}" for h in hits)
    return _JUDGE_PROMPT.format(entry_json=entry_json, hits_block=hits_block)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extract the last ``{...}`` JSON object from *text*."""
    start = text.rfind("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _coerce_verdict(data: dict[str, Any] | None, hits: list[SearchHit]) -> JudgeVerdict:
    """Normalize a raw judge payload — biased toward *unverified*.

    A non-dict payload, a ``citation_ref`` not among the hits, or a
    ``value_ok`` with no resolvable citation all degrade to
    ``value_ok=False`` — never a silent flip on malformed output.
    """
    valid = {h.handle for h in hits}
    if not isinstance(data, dict):
        return JudgeVerdict(False, None, "unparseable judge output")
    value_ok = data.get("value_ok") is True
    raw_ref = data.get("citation_ref")
    citation_ref = raw_ref if isinstance(raw_ref, str) and raw_ref in valid else None
    raw_note = data.get("note")
    note = raw_note.strip() if isinstance(raw_note, str) else ""
    if value_ok and citation_ref is None:
        return JudgeVerdict(False, None, note or "no resolvable citation among hits")
    return JudgeVerdict(value_ok, citation_ref, note)


def make_llm_judge_fn(*, tier: Any = None) -> JudgeFn:
    """The real LLM judge — one bounded MEDIUM-tier JSON call per entry,
    routed through the switchable router (ADR 0046)."""
    from precis.utils.llm.router import LlmRequest, Tier, dispatch

    resolved_tier = tier if tier is not None else Tier.MEDIUM

    def _judge(flagged: FlaggedEntry, hits: list[SearchHit]) -> JudgeVerdict:
        if not hits:
            return JudgeVerdict(False, None, "no corpus hits for this entry")
        prompt = _render_judge_prompt(flagged, hits)
        res = dispatch(
            LlmRequest(
                tier=resolved_tier,
                messages=[
                    {"role": "system", "content": _JUDGE_SYS},
                    {"role": "user", "content": prompt},
                ],
                prompt=prompt,
                source="sim:verify",
            )
        )
        if res.error:
            log.warning("sim verify: judge dispatch failed: %s", res.error)
            return JudgeVerdict(False, None, f"dispatch error: {res.error}")
        data = res.data or _parse_json_object(res.text)
        return _coerce_verdict(data, hits)

    return _judge


__all__ = [
    "DEFAULT_CONFIDENCE_FLOOR",
    "FileDiff",
    "FlaggedEntry",
    "JudgeFn",
    "JudgeVerdict",
    "SearchFn",
    "SearchHit",
    "VerifyOutcome",
    "VerifyRecord",
    "build_query",
    "make_corpus_search_fn",
    "make_llm_judge_fn",
    "plan_verify",
    "render_writebacks",
    "scan_entries",
    "verify_sim",
]
