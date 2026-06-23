"""Universal handle registry — the SSOT for ADR 0036 handles.

A *handle* is the one address form for every persistent ref and every
addressable body chunk: a 2-char lowercase **type code** + the row's
**decimal primary key**, e.g. ``pa5`` (a paper, ``ref_id`` 5), ``pc10``
(a paper chunk, ``chunk_id`` 10), ``tg42`` (a tag, ``tag_id`` 42). Bare
ASCII, variable length, self-delimiting (letters = type, digits = id).

**Computed, not stored.** A handle is a pure function of ``(kind, id)`` —
no handle column, no minting, no backfill, no migration.
:func:`format_handle` formats; ``Store.resolve_handle`` decodes the
prefix to a table + kind
and does a primary-key lookup. See
``docs/decisions/0036-universal-handles.md`` ("Final design").

Two kinds are *file-backed* (``skill`` → ``sk``, ``python`` → ``py``) and
one lives in its own table (``tag`` → ``tg``); they carry codes for
registry completeness but keep their existing ``kind`` + slug/id
addressing — folding them into ``resolve_handle`` is a later slice, so
:func:`parse` / :func:`is_well_formed` treat only the refs-backed
(record + chunk) decimal codes as resolvable handles. ``random`` is a
stateless generator and has **no handle**.

Why code-as-SSOT (not a hand-maintained ADR table): kinds slip through
manual diligence (``news``/``message``/``cron`` all did). The totality
test in ``tests/test_handle_registry.py`` asserts every persistent ref
kind has a code, so adding a kind without a code fails CI, not review.
"""

from __future__ import annotations

import re

CODE_LEN = 2

# --- record codes (the addressable persistent-ref kinds) ------------------
# Authoritative kind list: dispatch.boot() composition root. Providers
# (web/youtube/wikipedia/semanticscholar/perplexity-*) and stateless tools
# (calc/math/provenance/random) are addressed by URL/query/compute, not
# handles.

KIND_CODES: dict[str, str] = {
    # corpus / documents
    "paper": "pa",
    "patent": "pt",
    "news": "nw",
    "draft": "dr",
    "conv": "co",
    "pres": "pr",
    "markdown": "md",
    "plaintext": "pl",
    "tex": "tx",
    "python": "py",
    # thoughts / generated
    "memory": "me",
    "oracle": "or",
    "finding": "fi",
    "citation": "ci",
    "flashcard": "fc",
    # operational
    "todo": "td",
    "job": "jo",
    "alert": "al",
    "agentlog": "ag",
    "cron": "cr",
    "message": "ms",
    "gripe": "gr",
    # system / meta
    "skill": "sk",
    "tag": "tg",
}

# --- chunk codes (kinds that expose addressable body chunks) --------------
# A chunk gets its own flat handle (``<chunk-code><chunk_id>``); the doc
# relationship lives in a column. Convention: ``<initial>c`` where free,
# else a free mnemonic. Disjoint from KIND_CODES.

CHUNK_CODES: dict[str, str] = {
    "paper": "pc",
    "patent": "pk",
    "plaintext": "lc",
    "markdown": "mc",
    "tex": "xc",
    "news": "nc",
    "draft": "dc",
    "conv": "cc",
    "pres": "ps",
    "gripe": "gc",
    "message": "mb",
    "cron": "cp",
    "finding": "fb",
    "job": "jc",
}

# Reverse map (code -> (kind, is_chunk)).
_CODE_TO_KIND: dict[str, tuple[str, bool]] = {
    **{c: (k, False) for k, c in KIND_CODES.items()},
    **{c: (k, True) for k, c in CHUNK_CODES.items()},
}

# Codes that are NOT refs-backed: file-backed (slug/path body) or
# other-table. They keep their existing ``kind`` + id addressing, so
# ``resolve_handle`` / :func:`parse` don't treat them as decimal handles
# (yet). The codes still exist for registry completeness + the totality
# test.
_FILE_BACKED_KINDS = frozenset({"skill", "python"})
_OTHER_TABLE_KINDS = frozenset({"tag"})

#: Codes whose body is the row's decimal primary key and which
#: :func:`parse` can decode to a ``(kind, is_chunk, pk)`` triple —
#: every record code except the file-backed / other-table kinds, plus
#: every chunk code.
_DECIMAL_CODES: frozenset[str] = frozenset(
    {
        c
        for k, c in KIND_CODES.items()
        if k not in _FILE_BACKED_KINDS | _OTHER_TABLE_KINDS
    }
    | set(CHUNK_CODES.values())
)


# --- lookups --------------------------------------------------------------


def code_for_kind(kind: str, *, chunk: bool = False) -> str:
    """Return the 2-char code for ``kind`` (its chunk code if ``chunk``)."""
    table = CHUNK_CODES if chunk else KIND_CODES
    try:
        return table[kind]
    except KeyError:
        which = "chunk" if chunk else "record"
        raise KeyError(f"no {which} handle code for kind {kind!r}") from None


def kind_for_code(code: str) -> tuple[str, bool]:
    """Resolve a 2-char code to ``(kind, is_chunk)``."""
    try:
        return _CODE_TO_KIND[code.lower()]
    except KeyError:
        raise KeyError(f"unknown handle type code {code!r}") from None


# --- format & parse -------------------------------------------------------


def format_handle(kind: str, id_: int, *, chunk: bool = False) -> str:
    """Format the computed handle for ``(kind, id_)``.

    ``id_`` is the row's decimal primary key (``ref_id`` for a record,
    ``chunk_id`` for a chunk). Raises ``KeyError`` for a kind with no
    code — use :func:`try_format` on a hot path that may see one.
    """
    return code_for_kind(kind, chunk=chunk) + str(id_)


def try_format(kind: str, id_: int | None, *, chunk: bool = False) -> str | None:
    """Like :func:`format_handle`, but ``None`` instead of raising when
    ``kind`` has no code (or ``id_`` is ``None``). Used by the search
    emitters, which see every kind including the code-less providers."""
    if id_ is None:
        return None
    try:
        return format_handle(kind, id_, chunk=chunk)
    except KeyError:
        return None


def normalize(handle: str) -> str:
    """Canonicalise a handle: strip + lowercase the 2-char prefix.

    The decimal body has no case to fold; the prefix is lowercased so a
    shouted ``ME5`` still resolves. Does not validate.
    """
    s = handle.strip()
    if len(s) <= CODE_LEN:
        return s.lower()
    return s[:CODE_LEN].lower() + s[CODE_LEN:]


def parse(handle: str) -> tuple[str, bool, int] | None:
    """Decode a handle to ``(kind, is_chunk, pk)``, or ``None``.

    ``None`` for anything that is not a well-formed refs-backed decimal
    handle (unknown / file-backed / other-table code, non-digit body,
    legacy slug, …) — so a caller falls through to legacy id resolution
    untouched.
    """
    s = normalize(handle)
    if len(s) <= CODE_LEN:
        return None
    code, body = s[:CODE_LEN], s[CODE_LEN:]
    if code not in _DECIMAL_CODES or not body.isdigit():
        return None
    kind, is_chunk = _CODE_TO_KIND[code]
    return kind, is_chunk, int(body)


def is_well_formed(handle: str) -> bool:
    """True iff ``handle`` is a resolvable refs-backed decimal handle."""
    return parse(handle) is not None


# --- relative navigation grammar (ADR 0036) -------------------------------
# A *relative* handle is a chunk handle plus ONE trailing operator, resolved
# against current structure (never stored). The store walks it to a target
# chunk; see ``Store.resolve_relative``.

#: ``<chunk-handle><operator>`` — base is ``<2-char code><digits>``; the
#: operator is everything after the leading digit run.
_REL_RE = re.compile(r"^([a-z]{2})(\d+)(.+)$")

#: A parsed relative operator. One of:
#:   ("step", n)         sibling step (signed n, e.g. +1 / -3)
#:   ("ancestor", n)     n levels up (n >= 1)
#:   ("span", lo, hi)    signed sibling span, anchor at 0 (e.g. -2..3)
RelOp = tuple


def _parse_op(op: str) -> RelOp | None:
    """Parse one trailing operator (``+1`` / ``-3`` / ``^`` / ``^2`` / ``++`` /
    ``--`` / ``^^`` / ``-2..3``) into a typed tuple, or ``None`` if malformed."""
    if op in ("++", "--"):
        return ("step", 1 if op == "++" else -1)
    if op == "^^":
        return ("ancestor", 2)
    if op == "^":
        return ("ancestor", 1)
    if ".." in op:  # signed span: lo..hi
        lo_s, _, hi_s = op.partition("..")
        try:
            return ("span", int(lo_s), int(hi_s))
        except ValueError:
            return None
    m = re.fullmatch(r"\^(\d+)", op)
    if m:
        n = int(m.group(1))
        return ("ancestor", n) if n >= 1 else None
    m = re.fullmatch(r"([+-]\d+)", op)
    if m:
        n = int(m.group(1))
        return ("step", n) if n != 0 else None
    return None


def parse_relative(handle: str) -> tuple[str, bool, int, RelOp] | None:
    """Decode a relative chunk handle to ``(kind, is_chunk, pk, op)``.

    ``None`` when ``handle`` carries no (valid) trailing operator — i.e. it
    is an absolute handle (use :func:`parse`) or junk. Only refs-backed
    chunk codes are eligible (relative nav walks chunk structure).
    """
    m = _REL_RE.match(normalize(handle))
    if m is None:
        return None
    code, digits, op_str = m.group(1), m.group(2), m.group(3)
    if code not in _DECIMAL_CODES:
        return None
    kind, is_chunk = _CODE_TO_KIND[code]
    if not is_chunk:  # relative grammar is chunk-level only
        return None
    op = _parse_op(op_str)
    if op is None:
        return None
    return kind, is_chunk, int(digits), op
