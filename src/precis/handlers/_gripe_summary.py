"""The gripe's current-state summary, rebuilt from the comment ledger (gr595551).

A gripe's ``gripe_comment`` chunks are the append-only ledger — write
once, read many. Every pipeline stage (triage, test, code) appends its
entry there and never edits it. The *summary* is the one mutable block:
``refs.meta['summary']`` = ``{text, through, at, by}``, where ``through``
is the newest comment ord the rebuild covered — a reader can tell at a
glance whether the summary is current (the card marks it ``stale`` once
the ledger has moved on). Dead ends stay in the summary as one line each
("``ruled out: <what>, see entry N``"), so the next agent does not
re-investigate them.

Code locations in ledger entries and in the summary are code-anchor
handles only — ``an<N>`` (gr595549; ``co<N>`` was the report's earlier
spelling — both are gated), never paths, never snippets. A handle
resolves through the ``alias`` kind — precis's durable short-string
ledger: ``put(kind='alias', args={'a': 'an12', 'b':
'src/precis/flux/parse.py::deflux_core', 'rel': 'short-for'})`` records
the pair (gr595549's python-kind mechanism mints the same map once it
lands; :func:`resolve_anchors` is the one seam to re-point).

Rebuilding is a small-model task — old summary + new entry → new
summary (:func:`rebuild_summary`, ``Tier.SMALL`` via :func:`route_small`,
injectable for tests and workers). The fold walks the new entries one
at a time; an entry past :data:`_ENTRY_CAP` is fed as sequential
``part i/n`` pieces, so every byte reaches the model and the
``through`` stamp never claims coverage of text it did not see (a
truncated entry would permanently skip the evidence — and the dead
ends — it dropped). Folds persist per entry: a mid-rebuild refusal
leaves an honest (text, through) pair the next call resumes from. Three
hard gates keep the model honest (:func:`gate_summary`):

- every anchor handle in the new summary must appear in the ledger —
  the summary can never cite a code location no entry named;
- every ``entry N`` / ``comment N`` reference must resolve to a comment
  ord that exists — the "see entry N" pointers stay inside the ledger;
- no raw code location (``path`` / ``path::qual``) may appear at all —
  the summary's contract is anchors only, same as the ledger's.

Surfaces: the card block (:func:`card_lines`) and ``view='summary'``
(:func:`render_summary_view`) read it; ``put(id=N, text=…,
args={'stage': '<slug>'})`` appends a stage-marked entry (``chunks.meta
['stage']``) and rebuilds — a stage entry still naming a raw location
refuses, the handle mints first
(``put(kind='alias', args={'a': 'an<N>', 'b': '<path[::qual]>', 'rel':
'short-for'})``); ``edit(mode='summary')`` rebuilds on demand —
``args={'summary': '<text>'}`` writes a vetted text with no model call
(same gate), ``args={'summary': ''}`` clears. ``get(id='/match',
args={'trace': '<stack trace>'})`` is the integration-failure ticket
matcher: it ranks open gripes by which trace symbols/files their ledger
already names, and the newest touched-files entry says which files
belong to which gripe (:func:`match_rows`, :func:`render_match`; a
touched-files entry carries a line opening with the literal
``touched files:`` marker, and only that line's payload supplies the
row's file list — a file on the NEXT line is hit evidence, never
ownership). An entry's match surface is its literal text plus
the locations its anchor handles resolve to, all case-folded — an
anchor-only entry still identifies failing code.

Producers mint their handles through :func:`mint_anchors`
(find-or-mint over the same alias map, optionally linked to the gripe
as provisional pairs) — the write half of the one seam, symmetric to
:func:`resolve_anchors` — so the ledger never sees a raw path or
symbol. Allocation runs its max-over-ALL-pairs scan and the inserts in
one transaction behind a ``pg_advisory_xact_lock``: two concurrent
minters can never mint the same handle, and a retired or expired
handle's number is history — never re-minted for a new location.
:func:`anchorize_text` is the free-text companion — a stage whose
entry names locations in prose (the triage tick's suspected cause)
has each ``path``/``path::qual`` token minted and rewritten to its
handle before the entry lands; :func:`redact_locations` is the
fail-soft floor when the mint is down.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers import _comment_timeline as tl

if TYPE_CHECKING:
    from precis.store.types import ChunkRow, Ref

#: ``refs.meta`` key holding the mutable summary block.
SUMMARY_KEY = "summary"
#: ``chunks.meta`` key a ``put(args={'stage': …})`` entry carries.
STAGE_META_KEY = "stage"
#: The ledger kind — the gripe's append-only comment chunks.
COMMENT_KIND = "gripe_comment"

#: Code-anchor handle tokens (gr595549). ``an<N>`` is the ruled spelling;
#: ``co<N>`` the report's earlier one — both are code anchors, and either
#: must exist in the ledger before a summary may cite it.
ANCHOR_TOKEN_RE = re.compile(r"\b(?:an|co)\d+\b")
#: ``entry N`` / ``comment N`` pointers the "ruled out: …, see entry N"
#: lines carry — gated against the ledger's comment ords.
ENTRY_REF_RE = re.compile(r"\b(?:entry|comment)\s+#?(\d+)\b", re.IGNORECASE)

#: ``args={'stage': '<slug>'}`` vocabulary is open, the slug shape is not.
STAGE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

#: Card render budget for the summary text; past it the block clips and
#: names ``view='summary'`` (the uncapped read).
CARD_SUMMARY_CHARS = 900

_LLM_SOURCE = "gripe_summary:rebuild"


@dataclass(frozen=True)
class Summary:
    """``meta.summary`` parsed: the mutable block on a WORM ledger."""

    text: str
    # Newest comment ord the rebuild covered. Ord 0 is a real entry —
    # worker writers mint the first ord as 0 — so "no summary yet" is
    # this block's ABSENCE, not through=0; the fold's "nothing folded"
    # floor is -1 (never stored).
    through: int
    at: str | None  # UTC stamp of the last rebuild
    by: str  # 'small-model' or 'agent'


def summary_of(meta: dict[str, Any] | None) -> Summary | None:
    """``refs.meta['summary']`` → a :class:`Summary`, or ``None``."""
    raw = (meta or {}).get(SUMMARY_KEY)
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        n = int(raw.get("through") or 0)
    except (TypeError, ValueError):
        n = 0
    by = raw.get("by")
    return Summary(
        text=text.strip(),
        through=max(0, n),
        at=raw.get("at") if isinstance(raw.get("at"), str) else None,
        by=by if isinstance(by, str) else "agent",
    )


def ledger(chunks: list[ChunkRow]) -> list[ChunkRow]:
    """The gripe's comment ledger: its comment chunks, ord order.

    Hidden comments stay — the ledger is WORM; a hidden entry is still
    history (and its anchors still exist for the gate)."""
    return sorted(
        (c for c in chunks if c.chunk_kind == COMMENT_KIND and c.ord >= 0),
        key=lambda c: c.ord,
    )


def stage_of(chunk: ChunkRow) -> str | None:
    s = (chunk.meta or {}).get(STAGE_META_KEY)
    return s if isinstance(s, str) and s else None


def newest_ord(comments: list[ChunkRow]) -> int:
    return max((c.ord for c in comments), default=0)


def freshness(summary: Summary | None, comments: list[ChunkRow]) -> str | None:
    """``'current'`` / ``'stale'`` against the ledger; ``None`` when no
    summary exists. A summary with nothing to summarise (empty ledger)
    still reads current."""
    if summary is None:
        return None
    return "current" if summary.through >= newest_ord(comments) else "stale"


# ── the hard gate ──────────────────────────────────────────────────────


def gate_summary(text: str, comments: list[ChunkRow]) -> list[str]:
    """Refusal reasons for ``text`` as the new summary; ``[]`` = clean.

    Every anchor handle quoted must exist somewhere in the ledger; every
    ``entry N`` pointer must be a live comment ord; and no raw code
    location (``path`` / ``path::qual``) may appear at all — the
    summary's contract is anchors only, same as the ledger's (review of
    td655156)."""
    ledger_text = "\n".join(c.text for c in comments)
    ledger_handles = {h.lower() for h in ANCHOR_TOKEN_RE.findall(ledger_text)}
    ords = {c.ord for c in comments}
    problems: list[str] = []
    missing = sorted(
        {h.lower() for h in ANCHOR_TOKEN_RE.findall(text)} - ledger_handles
    )
    problems += [f"anchor {h} is not in the ledger" for h in missing]
    bad_ords = sorted({int(n) for n in ENTRY_REF_RE.findall(text)} - ords)
    problems += [f"entry {n} is not a ledger comment" for n in bad_ords]
    problems += [
        f"raw location {loc} — the summary cites an<N> handles"
        for loc in extract_locations(text)
    ]
    return problems


def _gate_refusal(problems: list[str], ref_id: int) -> BadInput:
    return BadInput(
        "summary gate refused the new text: " + "; ".join(problems),
        next=(
            "quote only code anchors and entry numbers that appear in the "
            f"comment ledger (get(kind='gripe', id={ref_id}, "
            "view='comments')) — mint a location's handle first: "
            "put(kind='alias', args={'a': 'an<N>', 'b': '<path[::qual]>', "
            "'rel': 'short-for'})"
        ),
    )


# ── the small-model rebuild ────────────────────────────────────────────

_PROMPT = """You maintain the current-state summary of a bug-tracker entry.

The gripe's comments are an append-only ledger; the summary is the one
mutable block a reader sees first so it does not have to replay every
entry. Rewrite it for the new entries below.

Rules:
- Reply with the new summary text only — no preamble, no headings, no
  fences. At most 1500 characters; short lines.
- Keep the state that still stands; drop only what the new entries
  supersede.
- Every ruled-out hypothesis survives as one line:
  "ruled out: <what>, see entry <N>".
- Code locations are code-anchor handles (an<N>) only — never paths,
  never snippets. Every handle you write must appear in the ledger.
- Refer to ledger entries as "entry N" (their comment numbers).
"""

#: One prompt piece's text budget. A longer entry folds as sequential
#: ``part i/n`` pieces rather than being truncated — cutting the tail
#: would let ``through`` claim coverage of evidence the model never
#: saw.
_ENTRY_CAP = 4000


def _entry_pieces(c: ChunkRow) -> list[str]:
    """One ledger entry → its prompt block(s): a single ``entry N``
    block, or sequential ``part i/n`` pieces past :data:`_ENTRY_CAP`."""
    stage = stage_of(c)
    tag = f" ({stage})" if stage else ""
    text = c.text.strip()
    if len(text) <= _ENTRY_CAP:
        return [f"--- entry {c.ord}{tag} ---\n{text}"]
    parts = [text[i : i + _ENTRY_CAP] for i in range(0, len(text), _ENTRY_CAP)]
    n = len(parts)
    return [
        f"--- entry {c.ord}{tag}, part {i + 1}/{n} ---\n{p}"
        for i, p in enumerate(parts)
    ]


def build_prompt(old: str, pieces: list[str]) -> str:
    """old summary + new ledger-entry piece(s) → the rebuild prompt."""
    return (
        _PROMPT
        + "\nCurrent summary (empty if none):\n"
        + (old.strip() or "(none)")
        + "\n\nNew ledger entries (oldest first):\n"
        + "\n".join(pieces)
    )


def route_small(prompt: str) -> str:
    """The real SMALL-tier call (``summarizer`` lane) — module-level so a
    test or worker can inject its own ``llm=`` into
    :func:`rebuild_summary`."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    res = route(
        LlmRequest(
            tier=Tier.SMALL,
            prompt=prompt,
            source=_LLM_SOURCE,
            work_class="background",
            timeout_s=120.0,
            max_tokens=1024,
        )
    )
    if res.error:
        from precis.errors import Upstream

        raise Upstream(
            f"the summary rebuild's small-model call failed: {res.error}",
            next="retry edit(kind='gripe', id=<id>, mode='summary'), or "
            "write the text yourself with args={'summary': '<text>'}",
        )
    return res.text


def _clean_reply(reply: str) -> str:
    """Strip the small model's reply to the summary text: whitespace and
    one optional code fence, nothing else."""
    text = reply.strip()
    m = re.match(r"^```[a-z]*\n(.*)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    return text


def _write(store: Any, ref_id: int, text: str, through: int, by: str) -> None:
    store.stamp_ref_meta(
        ref_id,
        {
            SUMMARY_KEY: {
                "text": text,
                "through": through,
                "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"),
                "by": by,
            }
        },
    )


def rebuild_summary(
    store: Any,
    ref: Ref,
    *,
    llm: Callable[[str], str] | None = None,
) -> str:
    """Fold the ledger entries newer than ``through`` into the summary —
    one entry (or entry-piece) per small-model call, the spec's "old
    summary + new entry → new summary"; returns the ack text.

    ``through`` advances only past an entry whose WHOLE text was folded
    — every ``part i/n`` piece included — so the coverage stamp never
    claims evidence the model did not see. Each completed entry's fold
    persists before the next begins: a mid-rebuild refusal still leaves
    an honest ``(text, through)`` pair the next call resumes from.
    Raises :class:`BadInput` (gate refusal or an empty reply — folds
    already persisted stand) or :class:`Upstream` (the model call
    failed, same rule)."""
    comments = ledger(store.chunks.list_chunks_for_ref(ref.id))
    if not comments:
        raise BadInput(
            f"gripe id={ref.id} has no ledger entries — nothing to summarise",
            next=f"put(kind='gripe', id={ref.id}, text='<entry>', "
            "args={'stage': '<slug>'}) appends the first",
        )
    old = summary_of(ref.meta)
    # ``-1`` is the "nothing folded yet" floor: worker writers mint the
    # first ledger ord as 0 (``gripe_triage._next_ord``,
    # ``executors._common.append_chunk``), so a 0 floor would skip a
    # chunkless gripe's ord-0 entry forever (review of td655156).
    through = old.through if old else -1
    new_entries = [c for c in comments if c.ord > through]
    if not new_entries:
        return f"gripe id={ref.id}: summary already current (through comment {through})"
    call = llm or route_small
    text = old.text if old else ""
    for c in new_entries:
        for piece in _entry_pieces(c):
            text = _clean_reply(call(build_prompt(text, [piece])))
            if not text:
                raise BadInput(
                    "the small model returned an empty summary — folds "
                    f"through comment {through} stand",
                    next=f"retry edit(kind='gripe', id={ref.id}, "
                    "mode='summary'), or write it with "
                    "args={'summary': '<text>'}",
                )
            problems = gate_summary(text, comments)
            if problems:
                raise _gate_refusal(problems, ref.id)
        through = c.ord
        _write(store, ref.id, text, through, "small-model")
    n = len(new_entries)
    return (
        f"gripe id={ref.id}: summary rebuilt through comment {through} "
        f"(folded {n} entr{'y' if n == 1 else 'ies'} "
        f"{new_entries[0].ord}-{through})"
    )


def write_summary_text(
    store: Any,
    ref: Ref,
    text: str,
    *,
    through: int | None = None,
    dry_run: bool = False,
) -> str:
    """``args={'summary': '<text>'}`` — the no-model write, same gate.

    ``through`` defaults to the newest ledger ord; ``''`` clears the
    block (a fresh start, not a stale mark)."""
    comments = ledger(store.chunks.list_chunks_for_ref(ref.id))
    top = newest_ord(comments)
    if text == "":
        if dry_run:
            return f"dry run: gripe id={ref.id} summary cleared (nothing written)"
        store.clear_ref_meta_keys(ref.id, [SUMMARY_KEY], source="agent")
        return f"gripe id={ref.id}: summary cleared"
    if through is not None and (through < 0 or through > top):
        raise BadInput(
            f"args['through']={through} is outside the ledger "
            f"(0..{top} comment{'s' if top != 1 else ''})",
            next=f"get(kind='gripe', id={ref.id}, view='comments')",
        )
    problems = gate_summary(text, comments)
    if problems:
        raise _gate_refusal(problems, ref.id)
    n = through if through is not None else top
    if dry_run:
        return (
            f"dry run: gripe id={ref.id} summary {n and f'through comment {n}'}"
            " (nothing written)"
        )
    _write(store, ref.id, text.strip(), n, "agent")
    return f"gripe id={ref.id}: summary written (through comment {n})"


# ── rendering ──────────────────────────────────────────────────────────


def _fresh_note(ref_id: int, summary: Summary, comments: list[ChunkRow]) -> str:
    fresh = freshness(summary, comments)
    if fresh == "stale":
        return (
            f"through comment {summary.through} · STALE — ledger at "
            f"{newest_ord(comments)} · rebuild: "
            f"edit(kind='gripe', id={ref_id}, mode='summary')"
        )
    return f"through comment {summary.through} · current"


def card_lines(ref: Ref, comments: list[ChunkRow]) -> list[str]:
    """The bare-get summary block — ``[]`` when no summary exists yet."""
    s = summary_of(ref.meta)
    if s is None:
        return []
    note = _fresh_note(ref.id, s, comments)
    body = tl.clip_middle(
        s.text,
        CARD_SUMMARY_CHARS,
        f"get(kind='gripe', id={ref.id}, view='summary')",
    )
    return ["", f"summary ({note}):", body]


def render_summary_view(ref: Ref, head: str, comments: list[ChunkRow]) -> str:
    """``view='summary'``: the uncapped block plus its freshness."""
    s = summary_of(ref.meta)
    if s is None:
        return (
            f"{head}\nsummary: none — a stage appends its entry with "
            "put(kind='gripe', id="
            f"{ref.id}, text=…, args={{'stage': '<slug>'}}) and the put "
            "rebuilds it; on demand: "
            f"edit(kind='gripe', id={ref.id}, mode='summary')"
        )
    note = _fresh_note(ref.id, s, comments)
    by = f" · {s.by} {s.at}" if s.at else ""
    return f"{head}\nsummary ({note}{by}):\n\n{s.text}"


# ── /match: the integration-failure ticket ─────────────────────────────

#: File-ish tokens in a trace or a ledger entry — the repo's code and
#: doc shapes plus the common ones a stack trace could name. This is
#: also :func:`extract_locations`'s bare-path source, so an extension
#: missing here is a raw path that lands in the ledger ungated (review
#: of td655156).
_FILE_RE = re.compile(
    r"(?:[\w.-]+/)*[\w.-]+\.(?:pyi?|sql|ya?ml|toml|jsonl?|md|tsx?|jsx?|mjs|cjs"
    r"|css|html?|j2|sh|txt|ini|cfg|conf|env|service|plist|desktop|tex|rs|go"
    r"|rb|xml|csv|tsv|svg|cad|trig|extxyz|typed|hx)",
    re.IGNORECASE,
)
#: Identifier tokens worth matching: ``::``-qualified, snake_case or
#: CamelCase, len ≥ 4 — a bare english word is not a code location.
_SYM_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_TRACE_NOISE = frozenset(
    {
        "traceback",
        "file",
        "line",
        "error",
        "exception",
        "warning",
        "raise",
        "self",
        "none",
        "true",
        "false",
        "return",
        "assert",
        "module",
        "object",
        "type",
        "value",
        "with",
        "from",
        "import",
    }
)


def _sym_ok(tok: str) -> bool:
    """Worth matching: snake_case or CamelCase, len ≥ 4, not trace noise."""
    if tok.lower() in _TRACE_NOISE or len(tok) < 4:
        return False
    return "_" in tok or tok[0].isupper()


def trace_tokens(trace: str) -> tuple[set[str], set[str]]:
    """``(files, symbols)`` a stack trace names.

    Files keep both the written path and its basename (a ledger entry
    may write either); symbols are ``::``-qualified parts plus
    snake_case/CamelCase identifiers — both matched case-insensitively
    as substrings."""
    files: set[str] = set()
    for m in _FILE_RE.findall(trace):
        low = m.lower()
        files.add(low)
        files.add(low.rsplit("/", 1)[-1])
    syms: set[str] = set()
    for m in re.findall(r"[A-Za-z_][\w.]*::[\w.]+", trace):
        for part in re.split(r"::|\.", m):
            if _sym_ok(part):
                syms.add(part.lower())
    for m in _SYM_RE.findall(trace):
        if _sym_ok(m):
            syms.add(m.lower())
    return files, syms


#: A code stage's touched-files entry: a line opening with the literal
#: ``touched files:`` marker (optionally behind the fixer's ``FIXER
#: (auto):`` prefix). A line merely *beginning* ``touched`` (``touched
#: nothing in diagnostic.py``) is not an ownership entry (review of
#: td655156); a triage or test entry that merely names a file is
#: evidence for the hit count, never the ownership list. The gaps are
#: ``[ \t]*``, never ``\s*`` — whitespace matching must stay on the
#: marker's own line, or ``touched files:\n diagnostic.py`` would hand
#: the next line's file to ownership (review of td655156).
_TOUCHED_RE = re.compile(
    r"^[ \t]*(?:fixer \(auto\):[ \t]*)?touched files:[ \t]*",
    re.IGNORECASE | re.MULTILINE,
)


def touched_payload(text: str) -> str | None:
    """The payload of an entry's ``touched files:`` line, or ``None``.

    Ownership reads only this line's payload — a diagnostic line in the
    same entry naming another file never joins the file list."""
    m = _TOUCHED_RE.search(text)
    if m is None:
        return None
    return text[m.end() :].split("\n", 1)[0].strip()


def is_touched_entry(text: str) -> bool:
    """True when a ledger entry carries a ``touched files:`` line."""
    return _TOUCHED_RE.search(text) is not None


def _file_tokens(text: str) -> list[str]:
    return sorted(set(_FILE_RE.findall(text.lower())))


def resolve_anchors(store: Any, handles: set[str]) -> dict[str, str]:
    """``an<N>``/``co<N>`` → the code-location string each handle names.

    The map is the ``alias`` kind — precis's durable short-string
    ledger (gr595549's python-kind mechanism mints the same
    ``handle ⇄ <path>::<symbol>`` pairs once it lands; until then
    ``put(kind='alias', args={'a': 'an12', 'b': '<path::sym>'})`` is
    the mint). Whichever side was registered maps to the OTHER side,
    which is the location text the match scores. Live pairs only —
    expired ones are hidden like the kind's own lookup. Injectable:
    tests and workers may pass ``resolve=`` to :func:`match_rows`.
    """
    if not handles:
        return {}
    wanted = sorted(h.lower() for h in handles)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->'alias'->>'a', meta->'alias'->>'b' FROM refs "
            "WHERE kind = 'alias' AND retired_at IS NULL "
            "AND (meta->'alias'->>'expires_at' IS NULL "
            "     OR (meta->'alias'->>'expires_at')::timestamptz > now()) "
            "AND (lower(meta->'alias'->>'a') = ANY(%s) "
            "     OR lower(meta->'alias'->>'b') = ANY(%s))",
            (wanted, wanted),
        ).fetchall()
    out: dict[str, str] = {}
    for a, b in rows:
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        for side, other in ((a, b), (b, a)):
            if side.lower() in handles:
                out[side.lower()] = other
    return out


#: ``pg_advisory_xact_lock`` namespace for :func:`mint_anchors`'s
#: allocate-then-insert critical section — the two-int form keys
#: ``(namespace, 0)`` on ONE fixed key so every mint serialises. Without
#: it two concurrent minters read the same max and mint the same handle
#: (review of td655156). Distinct from the store's other namespaces
#: (``_cad_ops.FIELD_PUT_LOCK_NAMESPACE``).
ANCHOR_MINT_LOCK_NAMESPACE = 0xA11C  # "alloc"


def mint_anchors(
    store: Any, locations: Iterable[str], *, gripe: int | None = None
) -> dict[str, str]:
    """Find-or-mint ``an<N>`` handles for each code location; returns
    ``{location: handle}``.

    ``locations`` are anchor-shaped strings — ``path/to/file.py`` or
    ``path/to/file.py::Qual.name``. A location a LIVE pair already
    names reuses its handle; a new one allocates the next ``an<N>``
    past every pair the map has EVER held — retired and expired pairs
    count, so a dead handle's number is history, never re-minted for a
    different location (``co<N>`` rows count as taken too — same token
    space). The scan and the inserts run in one transaction behind
    :data:`ANCHOR_MINT_LOCK_NAMESPACE`'s advisory lock — a concurrent
    mint waits rather than minting a duplicate — and commit or roll
    back together. The inserted row is the same pair
    ``put(kind='alias', args={'a': 'an<N>', 'b': loc, 'rel':
    'short-for'})`` mints, so :func:`resolve_anchors` resolves it
    either way.

    ``gripe`` stamps ``meta.alias.gripe`` and the ``related-to`` /
    ``provisional-alias`` link ``put`` adds — the pair is provisional by
    design: it lands on ``/stale`` once the gripe closes and
    gr595549's python-kind mechanism replaces the map. Callers on
    layers that cannot import :mod:`precis.handlers` reach this through
    the :mod:`precis.utils.gripe_summary` seam. Requires the ``alias``
    kind registered (any booted store has it).
    """
    locs: list[str] = []
    seen: set[str] = set()
    for loc in locations:
        s = str(loc).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            locs.append(s)
    if not locs:
        return {}
    out: dict[str, str] = {}
    with store.tx() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (ANCHOR_MINT_LOCK_NAMESPACE, 0),
        )
        # ``live`` marks the pairs find-or-mint may reuse; ``top`` is
        # over ALL pairs — a retired/expired an<N>'s number stays spent.
        rows = conn.execute(
            "SELECT meta->'alias'->>'a', meta->'alias'->>'b', "
            "(retired_at IS NULL "
            " AND (meta->'alias'->>'expires_at' IS NULL "
            "      OR (meta->'alias'->>'expires_at')::timestamptz > now())) "
            "AS live "
            "FROM refs WHERE kind = 'alias'"
        ).fetchall()
        by_location: dict[str, str] = {}
        top = 0
        for a, b, live in rows:
            if not isinstance(a, str) or not isinstance(b, str):
                continue
            for side, other in ((a.strip(), b.strip()), (b.strip(), a.strip())):
                if side and other and ANCHOR_TOKEN_RE.fullmatch(side.lower()):
                    if live:
                        by_location.setdefault(other.lower(), side)
                    top = max(top, int(side[2:]))
        n = top
        for loc in locs:
            if (handle := by_location.get(loc.lower())) is not None:
                out[loc] = handle
                continue
            n += 1
            handle = f"an{n}"
            payload: dict[str, Any] = {"a": handle, "b": loc, "rel": "short-for"}
            if gripe is not None:
                payload["gripe"] = int(gripe)
            ref = store.insert_ref(
                kind="alias",
                slug=None,
                title=f"{handle} ⇄ {loc}"[:200],
                meta={"alias": payload},
                conn=conn,
            )
            if gripe is not None:
                store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=int(gripe),
                    relation="related-to",
                    meta={"role": "provisional-alias"},
                    conn=conn,
                )
            by_location[loc.lower()] = handle
            out[loc] = handle
    return out


# ── anchorize: free text → handles ────────────────────────────────────

#: A ``::``-qualified code location — ``path::qual`` or ``mod::qual`` —
#: a stage entry's free text may carry. Each mints an ``an<N>`` handle
#: before the entry lands (the ledger's anchor-only contract).
_LOCATION_QUAL_RE = re.compile(r"[\w./-]+::[\w.]+")
#: Edge punctuation trimmed off a matched location token — a ``::``
#: token's ``[\w.]+`` tail otherwise keeps trailing sentence
#: punctuation (``parse.py::f.``).
_TOKEN_EDGE = ".,;:)"
#: What an unminted location becomes in an entry — never the raw
#: token (the contract is anchors only, gr595551).
UNMINTED_LOCATION = "(unminted location)"


def _location_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, token)`` of each code location in ``text``, in
    order — ``path::qual``/``mod::qual`` tokens and bare file paths.
    A bare symbol (``deflux_core``) is prose, not a location."""
    spans: list[tuple[int, int, str]] = []
    for m in _LOCATION_QUAL_RE.finditer(text):
        s, e = m.start(), m.end()
        while e > s and text[e - 1] in _TOKEN_EDGE:
            e -= 1
        if e > s:
            spans.append((s, e, text[s:e]))
    quals = [(s, e) for s, e, _t in spans]
    for m in _FILE_RE.finditer(text):
        s, e = m.start(), m.end()
        if any(s < b and a < e for a, b in quals):
            continue  # overlaps a ``path::qual`` token already taken
        spans.append((s, e, text[s:e]))
    return sorted(spans)


def extract_locations(text: str) -> list[str]:
    """The code locations ``text`` names — ``::``-qualified tokens and
    bare file paths — deduped, first-appearance order. These are what
    :func:`anchorize_text` mints and substitutes."""
    out: list[str] = []
    seen: set[str] = set()
    for _s, _e, tok in _location_spans(text):
        if tok.lower() not in seen:
            seen.add(tok.lower())
            out.append(tok)
    return out


def substitute_locations(text: str, handles: dict[str, str]) -> str:
    """Replace every code-location span in ``text`` with its handle —
    :data:`UNMINTED_LOCATION` when ``handles`` lacks it."""
    spans = _location_spans(text)
    if not spans:
        return text
    lower = {k.lower(): v for k, v in handles.items()}
    out = text
    for s, e, tok in reversed(spans):
        out = out[:s] + lower.get(tok.lower(), UNMINTED_LOCATION) + out[e:]
    return out


def redact_locations(text: str) -> str:
    """Every code location → :data:`UNMINTED_LOCATION` — the fail-soft
    floor when the mint itself is down: a raw path may never land in
    the ledger."""
    return substitute_locations(text, {})


def anchorize_text(store: Any, text: str, *, gripe: int | None = None) -> str:
    """Rewrite the code locations in ``text`` to ``an<N>`` handles —
    find-or-mint each via :func:`mint_anchors`, substitute the handle.
    The free-text half of the ledger's anchor-only contract
    (gr595551): a stage whose entry names ``path``/``path::qual``
    locations in prose (the triage tick's suspected cause) lands
    handles only. A location the mint returned no handle for redacts
    to :data:`UNMINTED_LOCATION`, never lands raw."""
    locs = extract_locations(text)
    if not locs:
        return text
    handles = mint_anchors(store, locs, gripe=gripe)
    return substitute_locations(text, handles)


@dataclass(frozen=True)
class MatchRow:
    """One open gripe whose ledger a trace token hits."""

    ref_id: int
    title: str
    status: str | None
    hits: int
    #: File tokens of the NEWEST touched-files ledger entry's own
    #: ``touched files:`` line payload — the "which files belong to this
    #: gripe" answer (literal tokens plus its resolved anchors). Empty
    #: when that payload resolves to nothing — the newest marker still
    #: owns the answer; an older entry never fills in.
    files: tuple[str, ...]
    touched_ord: int | None  # that entry's ord


def match_rows(
    store: Any,
    refs: list[Ref],
    statuses: dict[int, str | None],
    trace: str,
    *,
    resolve: Callable[[set[str]], dict[str, str]] | None = None,
) -> list[MatchRow]:
    """Score each open gripe by which trace tokens its ledger already
    names — most hits first. One bulk chunk read plus one bulk anchor
    resolution; a gripe with no hit drops out. An entry's match surface
    is its literal text PLUS the locations its ``an<N>``/``co<N>``
    handles resolve to — a documented anchor-only entry identifies
    failing code. Everything scores case-folded — trace tokens,
    ledger prose and resolved locations alike — so a mixed-case
    minted path (``an1 ⇄ src/App.tsx``) still hits the trace's
    ``File "src/App.tsx"``. ``files``/``touched_ord`` come from the
    ``touched files:`` line's payload (:func:`touched_payload`) in the single
    newest touched-files entry (:func:`is_touched_entry`), never the
    union, never the rest of that comment, and never a later
    diagnostic entry that merely names a file. The newest marker
    decides even when its payload resolves to no files — an empty or
    unresolved payload answers "none", it does not defer to older
    ownership."""
    files, syms = trace_tokens(trace)
    if not files and not syms:
        return []
    ids = [r.id for r in refs]
    if not ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, ord, text FROM chunks WHERE ref_id = ANY(%s) "
            "AND chunk_kind = %s AND ord >= 0 AND retired_at IS NULL "
            "ORDER BY ref_id, ord",
            (ids, COMMENT_KIND),
        ).fetchall()
    ledger_by_ref: dict[int, list[tuple[int, str]]] = {}
    for ref_id, ord_, text in rows:
        ledger_by_ref.setdefault(int(ref_id), []).append((int(ord_), str(text)))
    handles = {
        h.lower()
        for entries in ledger_by_ref.values()
        for _o, t in entries
        for h in ANCHOR_TOKEN_RE.findall(t)
    }
    resolved = (resolve or (lambda hs: resolve_anchors(store, hs)))(handles)
    # Resolved locations join the same case-folded surface the trace
    # tokens and ledger prose score against — a pair minted as
    # ``an1 ⇄ src/App.tsx`` must still hit ``File "src/App.tsx"``
    # (review FIX of td655156).
    anchors = {h: loc.lower() for h, loc in resolved.items()}
    out: list[MatchRow] = []
    for ref in refs:
        entries = ledger_by_ref.get(ref.id, [])
        blobs = [
            t.lower()
            + "\n"
            + "\n".join(
                anchors[h]
                for h in {x.lower() for x in ANCHOR_TOKEN_RE.findall(t)}
                if h in anchors
            )
            for _o, t in entries
        ]
        blob = "\n".join(blobs)
        hits = sum(1 for f in files if f in blob) + sum(1 for s in syms if s in blob)
        if not hits:
            continue
        touched_ord: int | None = None
        last_files: tuple[str, ...] = ()
        for ord_, t in reversed(entries):
            payload = touched_payload(t)
            if payload is None:
                continue
            # Ownership comes from the ``touched files:`` line's payload
            # alone — its file tokens plus the locations its anchors
            # resolve to — never the whole comment (review of td655156).
            # The NEWEST marker owns the answer even when its payload
            # yields no files: an empty or unresolved payload means "no
            # files on record" — falling through to an older marker
            # would restore ownership the newest entry superseded
            # (review FIX of td655156).
            payload_blob = (
                payload.lower()
                + "\n"
                + "\n".join(
                    anchors[h]
                    for h in {x.lower() for x in ANCHOR_TOKEN_RE.findall(payload)}
                    if h in anchors
                )
            )
            touched_ord, last_files = ord_, tuple(_file_tokens(payload_blob))
            break
        out.append(
            MatchRow(
                ref_id=ref.id,
                title=" ".join((ref.title or "").split())[:80],
                status=statuses.get(ref.id),
                hits=hits,
                files=last_files,
                touched_ord=touched_ord,
            )
        )
    out.sort(key=lambda r: (-r.hits, r.ref_id))
    return out


def render_match(rows: list[MatchRow], *, n_open: int, trace_chars: int) -> str:
    """``get(id='/match', args={'trace': …})``: ranked open gripes whose
    ledgers name the trace's symbols/files."""
    from precis.utils.handle_registry import try_format

    head = (
        f"# gripe trace match — {len(rows)} of {n_open} open gripes hit "
        f"({trace_chars} chars of trace)"
    )
    lines = [head]
    for r in rows[:20]:
        handle = try_format("gripe", r.ref_id) or f"gr{r.ref_id}"
        bits = [handle, f"STATUS:{r.status or '?'}", f"hits {r.hits}"]
        if r.touched_ord is not None:
            if r.files:
                shown = ", ".join(r.files[:4])
                more = f" +{len(r.files) - 4}" if len(r.files) > 4 else ""
                bits.append(f"last files entry {r.touched_ord}: {shown}{more}")
            else:
                bits.append(f"last files entry {r.touched_ord}: (none resolved)")
        bits.append(r.title)
        lines.append("  ".join(bits))
    if not rows:
        lines.append(
            "no open gripe's ledger names these symbols/files — file a new "
            "one: put(kind='gripe', text='<the failure>')"
        )
    lines.append(
        "match is the trace's file/symbol tokens against each open gripe's "
        "comment ledger; the last files entry says which files belong to it"
    )
    return "\n".join(lines)


__all__ = [
    "ANCHOR_TOKEN_RE",
    "CARD_SUMMARY_CHARS",
    "COMMENT_KIND",
    "ENTRY_REF_RE",
    "STAGE_META_KEY",
    "STAGE_SLUG_RE",
    "SUMMARY_KEY",
    "UNMINTED_LOCATION",
    "MatchRow",
    "Summary",
    "anchorize_text",
    "build_prompt",
    "card_lines",
    "extract_locations",
    "freshness",
    "gate_summary",
    "is_touched_entry",
    "ledger",
    "match_rows",
    "mint_anchors",
    "newest_ord",
    "rebuild_summary",
    "redact_locations",
    "render_match",
    "render_summary_view",
    "resolve_anchors",
    "route_small",
    "stage_of",
    "substitute_locations",
    "summary_of",
    "touched_payload",
    "trace_tokens",
    "write_summary_text",
]
