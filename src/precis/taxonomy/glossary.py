"""Mirror ``docs/glossary.md`` into ``taxon`` + ``alias`` nodes (gr548266).

The glossary is the project's coined/overloaded-term ontology; without a
mirror, search cannot consult it. One ``taxon`` per bold entry in the entry
sections (``## Coined terms``, ``## Overloaded …``, ``## Projects & quests …``):

- ``refs.title`` = the term (the bold span);
- ``meta.definition`` = the gloss (text up to the LAST `` → `` — earlier
  arrows are part of the gloss, e.g. "gripe → diagnose → fix");
- ``meta.pointer`` = the start-here pointer after it;
- ``meta.file_mirror`` = ``{'path', 'line', 'namespace'}`` — the mirror
  identity, matched on ``namespace`` + exact term, never bare
  ``norm_name`` (``taproot`` the graph and ``TAPROOT`` the tag axis
  share a norm but are different terms);
- ``meta.aliases`` = every name in ``(legacy: X)`` / ``(lit: Y)`` markers.

Each alias also gets a ``kind='alias'`` row (``rel='same-as'``, scope
``repo-dev-glossary``); ``lit`` aliases carry ``meta.kind='literature'``.
Every mirrored taxon is tagged ``SPACE:repo-dev``.

Namespace isolation is the point: a repo-dev glossary term never merges
into a same-named science taxon — the importer matches on
``file_mirror.namespace`` and put-time dedup skips candidates in another
namespace (:func:`precis.taxonomy.nodes.mirror_namespace`). ``--sync``
soft-deletes namespace nodes whose term left the file; without it a
removed term's node is left alone. ``--force`` bypasses the blob-sha
fast-path: the git blob sha of the file is kept in ``app_state`` so an
unchanged file costs one read.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.reading.concepts import normalize_name, split_name_def

if TYPE_CHECKING:
    from precis.store import Ref, Store

#: ``meta.file_mirror.namespace`` for every node this mirror owns.
GLOSSARY_NAMESPACE = "repo-dev-glossary"
#: The repo-dev space tag every mirrored taxon carries (same as memory).
REPO_DEV_TAG = "SPACE:repo-dev"
#: ``meta->'alias'->>'scope'`` on the alias rows this mirror owns; doubles
#: as the "is this our row" marker so an unrelated same-name alias pair
#: elsewhere is never touched.
ALIAS_SCOPE = GLOSSARY_NAMESPACE
#: ``app_state`` key for the last successfully mirrored blob sha, keyed by
#: namespace so a second mirrored file would not collide.
_BLOB_KEY = "taxonomy.glossary_mirror.blob"

#: ``## `` headings that start an entry section (prefix match — the
#: overloaded section's real title is "Overloaded — which one?").
_SECTION_PREFIXES = ("Coined terms", "Overloaded", "Projects & quests")

_BULLET_RE = re.compile(r"^\s*-\s+\*\*(?P<term>.+?)\*\*(?P<rest>.*)$")
_HEADING_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_MARKER_RE = re.compile(r"\((?P<kind>legacy|lit):")
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)

_MAX_ALIAS_CHARS = 64
_MAX_ALIAS_WORDS = 6


@dataclass
class GlossaryEntry:
    """One parsed ``- **term** — gloss. → pointer`` line."""

    term: str
    gloss: str
    pointer: str
    line: int  # 1-based line in the file
    aliases: list[str] = field(default_factory=list)  # legacy + lit, in order
    lit: frozenset[str] = frozenset()  # subset of aliases from (lit: …)


@dataclass
class GlossaryReport:
    """Counts for one ``import_glossary`` pass."""

    path: str
    entries: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    retired: int = 0
    aliases_added: int = 0
    aliases_retired: int = 0
    skipped: bool = False  # blob sha unchanged — nothing read but the file
    dry_run: bool = False

    def summary(self) -> str:
        if self.skipped:
            return f"glossary mirror: up to date ({self.entries} entries)"
        parts = [
            f"{self.created} created",
            f"{self.updated} updated",
            f"{self.unchanged} unchanged",
            f"{self.retired} retired",
            f"{self.aliases_added} aliases added",
            f"{self.aliases_retired} aliases retired",
        ]
        head = "glossary mirror (dry run)" if self.dry_run else "glossary mirror"
        return f"{head}: {self.entries} entries — " + ", ".join(parts)


# ── parsing ─────────────────────────────────────────────────────────


def _skip_parens(s: str) -> str:
    """``s`` starts with ``(``; return the tail after its balanced ``)``."""
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[i + 1 :]
    return ""


def _split_gloss(rest: str) -> tuple[str, str, list[str]]:
    """``rest`` is the line after the leading ``**term**``. Skip the
    qualifier zone — ``("also called")``, ``/ **alt-name**``, rulings —
    then split ``gloss → pointer`` on the LAST `` → ``. Returns
    ``(gloss, pointer, extra_aliases)``; extra bold spans in the
    qualifier zone (``**a** / **b**`` entries) are alternate spellings."""
    r = rest.lstrip()
    extra: list[str] = []
    while r:
        if r.startswith("—"):
            r = r[1:].lstrip()
            break
        if r.startswith("/"):
            r = r[1:].lstrip()
            continue
        if r.startswith("("):
            r = _skip_parens(r).lstrip()
            continue
        m = _BOLD_RE.match(r)
        if m:
            extra.append(m.group(1).strip())
            r = r[m.end() :].lstrip()
            continue
        break
    if r.startswith("→"):
        # Empty gloss: ``- **term** — → pointer``.
        return "", r[1:].strip(), extra
    if " → " in r:
        gloss, _, pointer = r.rpartition(" → ")
    else:
        gloss, pointer = r, ""
    return gloss.strip(), pointer.strip(), extra


def _markers(line: str) -> list[tuple[str, str]]:
    """``(kind, body)`` for each ``(legacy: …)``/``(lit: …)`` marker;
    the body may hold nested parens (``(lit: degree-of-interest (DOI) …)``)."""
    out = []
    for m in _MARKER_RE.finditer(line):
        i = m.end()
        depth = 1
        j = i
        while j < len(line) and depth:
            if line[j] == "(":
                depth += 1
            elif line[j] == ")":
                depth -= 1
            j += 1
        out.append((m.group("kind"), line[i : j - 1] if depth == 0 else line[i:]))
    return out


def _marker_names(body: str) -> list[str]:
    """Alias candidates out of one marker body. A backticked segment
    contributes its spans (``the `tier:*` value`` → ``tier:*``); a plain
    segment must read as a name — short, no sentence punctuation — or it
    is commentary, not an alias (``an eye's extent is the degree-of-
    interest cut-off``)."""
    out = []
    for seg in re.split(r"[;,]", body):
        seg = seg.strip()
        if not seg:
            continue
        spans = [s.strip() for s in _BACKTICK_RE.findall(seg) if s.strip()]
        if spans:
            out.extend(spans)
            continue
        cand = _ARTICLE_RE.sub("", seg.strip("*_ \t\"'")).strip()
        if (
            not cand
            or "—" in cand
            or cand.endswith(".")
            or len(cand) > _MAX_ALIAS_CHARS
            or len(cand.split()) > _MAX_ALIAS_WORDS
        ):
            continue
        out.append(cand)
    return out


def parse_glossary(text: str) -> list[GlossaryEntry]:
    """Parse the entry sections of ``docs/glossary.md``.

    One entry per ``- **term** …`` bullet under a ``## `` heading that
    starts with one of ``_SECTION_PREFIXES``. Aliases dedupe on the
    normalized form, order preserved; the term itself is never its own
    alias.
    """
    entries: list[GlossaryEntry] = []
    in_section = False
    for n, line in enumerate(text.splitlines(), start=1):
        h = _HEADING_RE.match(line)
        if h:
            in_section = h.group("name").startswith(_SECTION_PREFIXES)
            continue
        if not in_section:
            continue
        m = _BULLET_RE.match(line)
        if not m:
            continue
        term = m.group("term").strip()
        if not term:
            continue
        gloss, pointer, extra = _split_gloss(m.group("rest"))
        term_norm = normalize_name(term)
        aliases: list[str] = []
        lit: set[str] = set()
        seen: set[str] = {term_norm}
        for name in extra:
            norm = normalize_name(name)
            if norm and norm not in seen:
                seen.add(norm)
                aliases.append(name)
        for kind, body in _markers(line):
            for name in _marker_names(body):
                norm = normalize_name(name)
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                aliases.append(name)
                if kind == "lit":
                    lit.add(name)
        entries.append(
            GlossaryEntry(
                term=term,
                gloss=gloss,
                pointer=pointer,
                line=n,
                aliases=aliases,
                lit=frozenset(lit),
            )
        )
    return entries


def glossary_blob_sha(path: Path) -> str:
    """The git blob object id of *path*'s working-tree bytes —
    ``sha1("blob <len>\\0" + content)``, what ``git hash-object``
    computes for an unfiltered file. Pure python, no git needed."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ── mirror ──────────────────────────────────────────────────────────


def _blob_key(namespace: str) -> str:
    return f"{_BLOB_KEY}.{namespace}"


def _mirrored(store: Store, namespace: str) -> dict[str, Ref]:
    """Live ``taxon`` rows in *namespace*, keyed by exact ``meta.name``.

    The key is the stored term verbatim, never ``norm_name``: the file
    carries pairs that normalize to the same string but are different
    terms — ``taproot`` (the claim graph) vs ``TAPROOT`` (the tag axis)
    — and a normalized key would merge them into one node. Two lines
    that name the same term exactly (``watch`` in Coined *and*
    Overloaded) share one node; the later line's gloss wins.
    """
    out: dict[str, Ref] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='taxon' "
            "AND retired_at IS NULL "
            "AND meta->'file_mirror'->>'namespace' = %(ns)s "
            "ORDER BY ref_id",
            {"ns": namespace},
        ).fetchall()
    for (rid,) in rows:
        ref = store.get_ref(kind="taxon", id=int(rid))
        if ref is None:
            continue
        name = str((ref.meta or {}).get("name") or "")
        out.setdefault(name, ref)
    return out


def list_mirrored_terms(store: Store, namespace: str = GLOSSARY_NAMESPACE) -> list[str]:
    """The mirrored terms, sorted — what ``precis taxonomy list`` prints
    and ``scripts/memory-lint`` compares the file against."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->>'name' FROM refs WHERE kind='taxon' "
            "AND retired_at IS NULL "
            "AND meta->'file_mirror'->>'namespace' = %(ns)s "
            "ORDER BY 1",
            {"ns": namespace},
        ).fetchall()
    return [str(r[0]) for r in rows]


def _alias_rows(store: Store, scope: str) -> dict[tuple[str, str], int]:
    """Live ``same-as`` alias rows in *scope*, ``(a, b) -> ref_id``."""
    out: dict[tuple[str, str], int] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->'alias'->>'a', meta->'alias'->>'b' "
            "FROM refs WHERE kind='alias' AND retired_at IS NULL "
            "AND meta->'alias'->>'rel' = 'same-as' "
            "AND coalesce(meta->'alias'->>'scope', '') = %(scope)s",
            {"scope": scope},
        ).fetchall()
    for rid, a, b in rows:
        out.setdefault((str(a), str(b)), int(rid))
    return out


def _ref_for_term(store: Store, namespace: str, term: str) -> Ref | None:
    """The live mirrored taxon for one exact term — the same
    namespace+name identity ``_mirrored`` keys on, for the node a
    ``put`` just minted (its ack ``Response.ref_id`` is unset)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='taxon' "
            "AND retired_at IS NULL "
            "AND meta->>'name' = %(term)s "
            "AND meta->'file_mirror'->>'namespace' = %(ns)s "
            "ORDER BY ref_id LIMIT 1",
            {"term": term, "ns": namespace},
        ).fetchone()
    return store.get_ref(kind="taxon", id=int(row[0])) if row else None


def _sync_aliases(
    store: Store,
    term: str,
    aliases: list[str],
    lit: frozenset[str],
    rows: dict[tuple[str, str], int],
    *,
    write: bool,
    report: GlossaryReport,
    scope: str = ALIAS_SCOPE,
) -> None:
    """Create the entry's missing ``same-as`` alias rows; retire the
    term's rows the file no longer names. ``rows`` is the scope's
    (a, b) -> ref_id map, updated in place."""
    from precis.handlers.alias import META_KEY, _short, _side

    term_side = _side(term, "term")
    wanted = {_side(a, "alias"): term_side for a in aliases}
    for key in wanted.items():
        if key in rows:
            continue
        a, b = key
        if write:
            meta: dict[str, Any] = {
                META_KEY: {"a": a, "b": b, "rel": "same-as", "scope": scope}
            }
            if a in lit:
                meta["kind"] = "literature"
            ref = store.insert_ref(
                kind="alias",
                slug=None,
                title=_short(f"{a} ⇄ {b}", 200),
                meta=meta,
            )
            rows[key] = ref.id
        report.aliases_added += 1
    # A row for THIS term the file no longer names is stale — including
    # the empty-``wanted`` case (every marker dropped from the line).
    wanted_keys = set(wanted.items())
    stale = [k for k in rows if k[1] == term_side and k not in wanted_keys]
    for key in stale:
        if write:
            store.retire_ref(rows.pop(key))
        else:
            rows.pop(key)
        report.aliases_retired += 1


def import_glossary(
    store: Store,
    path: Path,
    *,
    sync: bool = False,
    dry_run: bool = False,
    blob_sha: str | None = None,
    force: bool = False,
    namespace: str = GLOSSARY_NAMESPACE,
) -> GlossaryReport:
    """Mirror one glossary file into ``taxon`` + ``alias`` nodes.

    Idempotent: matched on ``file_mirror.namespace`` + exact term,
    so a re-import reports every entry ``unchanged``. ``sync`` soft-
    deletes namespace nodes whose term is gone from the file (a node for
    a term still present is only patched). ``blob_sha`` = the file's git
    blob sha: when it equals the sha stored after the last successful
    pass the import is a no-op (``report.skipped``); ``force`` bypasses.
    """
    from precis.dispatch import Hub
    from precis.handlers.taxon import TaxonHandler
    from precis.store.types import Tag
    from precis.taxonomy.nodes import EDITABLE_KEYS, taxon_card_text

    path = Path(path)
    entries = parse_glossary(path.read_text(encoding="utf-8"))
    report = GlossaryReport(path=str(path), entries=len(entries), dry_run=dry_run)
    if blob_sha and not force and store.get_setting(_blob_key(namespace)) == blob_sha:
        report.skipped = True
        return report

    write = not dry_run
    handler = TaxonHandler(hub=Hub(store=store))
    existing = _mirrored(store, namespace)
    alias_rows = _alias_rows(store, ALIAS_SCOPE)
    space_tag = Tag.parse_strict(REPO_DEV_TAG, kind="taxon")
    seen_terms: set[str] = set()
    # ``file_mirror.path`` is the repo-relative path for the shipped
    # glossary, the literal path for an out-of-tree file (a test fixture).
    repo_root = Path(__file__).resolve().parents[3]
    try:
        relpath = str(path.resolve().relative_to(repo_root))
    except ValueError:
        relpath = str(path)

    for entry in entries:
        seen_terms.add(entry.term)
        file_mirror = {"path": relpath, "line": entry.line, "namespace": namespace}
        ref = existing.get(entry.term)
        if ref is None:
            text = f"{entry.term} — {entry.gloss}"
            if split_name_def(text)[0] != entry.term:
                # A term carrying —/– would mint a truncated name and
                # duplicate on every re-import; fail loudly instead.
                raise BadInput(
                    f"glossary term {entry.term!r} contains —/– (line {entry.line})",
                    next="the taxon name parser splits on em/en-dash "
                    "— rename the term in the file",
                )
            report.created += 1
            if write:
                # dedup=False: the importer's own namespace+term match is
                # the identity rule; the file can hold an alias that is
                # itself a term (``minter`` / ``dispatch``) and that file
                # fact must mirror, not refuse.
                resp = handler.put(
                    text=text,
                    meta={
                        "aliases": list(entry.aliases),
                        "pointer": entry.pointer,
                        "file_mirror": file_mirror,
                    },
                    tags=[REPO_DEV_TAG],
                    dedup=False,
                )
                new = _ref_for_term(store, namespace, entry.term)
                if new is None:
                    raise RuntimeError(
                        f"put for {entry.term!r} minted no mirrored node: {resp.body!r}"
                    )
                existing[entry.term] = new
        else:
            meta = ref.meta or {}
            patch: dict[str, Any] = {
                k: v
                for k, v in {
                    "definition": entry.gloss,
                    "pointer": entry.pointer,
                    "aliases": list(entry.aliases),
                }.items()
                if k in EDITABLE_KEYS and meta.get(k) != v
            }
            moved = meta.get("file_mirror") != file_mirror
            missing_tag = not any(
                t.namespace == "closed"
                and t.prefix == "SPACE"
                and t.value == "repo-dev"
                for t in store.tags_for(ref.id)
            )
            if patch or moved or missing_tag:
                report.updated += 1
                if write:
                    # store-level edit: the handler path re-validates the
                    # merged meta and would refuse a stamp key
                    # (worktree/slice); the importer's namespace+term
                    # match already is the identity rule.
                    meta_patch = dict(patch)
                    if moved:
                        meta_patch["file_mirror"] = file_mirror
                    with store.tx() as conn:
                        store.update_ref(ref.id, meta_patch=meta_patch, conn=conn)
                        if "definition" in patch or "aliases" in patch:
                            store.chunks.upsert_card_combined(
                                ref.id,
                                taxon_card_text(
                                    str(meta.get("name") or ref.title),
                                    str(
                                        patch.get("definition", meta.get("definition"))
                                        or ""
                                    ),
                                    patch.get("aliases", meta.get("aliases")),
                                ),
                                conn=conn,
                            )
                    if missing_tag:
                        store.add_tag(ref.id, space_tag)
            else:
                report.unchanged += 1
        _sync_aliases(
            store,
            entry.term,
            entry.aliases,
            entry.lit,
            alias_rows,
            write=write,
            report=report,
        )

    if sync:
        for name, ref in existing.items():
            if name in seen_terms:
                continue
            report.retired += 1
            if write:
                handler.delete(
                    id=ref.id,
                    reason=f"term removed from {relpath} (glossary mirror --sync)",
                )
            # Its scope alias rows are stale the moment the term is gone.
            stale = [k for k in alias_rows if k[1] == str((ref.meta or {}).get("name"))]
            for key in stale:
                if write:
                    store.retire_ref(alias_rows.pop(key))
                else:
                    alias_rows.pop(key)
                report.aliases_retired += 1

    if write and blob_sha:
        store.set_setting(_blob_key(namespace), blob_sha)
    return report


__all__ = [
    "ALIAS_SCOPE",
    "GLOSSARY_NAMESPACE",
    "REPO_DEV_TAG",
    "GlossaryEntry",
    "GlossaryReport",
    "glossary_blob_sha",
    "import_glossary",
    "list_mirrored_terms",
    "parse_glossary",
]
