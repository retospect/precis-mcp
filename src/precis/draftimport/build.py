"""The import writer + dry-run.

``walk_document`` (structure) + ``demacro`` (inline cleanup) + the cite /
glossary / ref resolution come together here. The ``--dry-run`` path is
pure (no DB): it builds the full chunk tree, cleans every chunk, and —
crucially for shaking the import out — scans the cleaned prose for any
**residual LaTeX command** that no rule handled yet, tallied by frequency
with an example. That surfaces the long tail of weird macros to deal with
before a single row is written.

    uv run python -m precis.draftimport.build nano-computer.tex --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.draftimport.demacro import (
    demacro,
    extract_annotations,
    harvest_acronyms,
    harvest_macros,
    harvest_param_macros,
    labels_in,
    resolve_deferred,
    strip_annotations,
)
from precis.draftimport.resolve import build_keymap, import_glossary
from precis.draftimport.tex import (
    Chunk,
    bib_paths_in,
    document_body,
    flatten_inputs,
    parse_bib,
    walk_document,
)

#: A LaTeX control sequence (command or active escape).
_CMD = re.compile(r"\\[a-zA-Z@]+\*?|\\[^a-zA-Z\s]")
#: $…$ / $$…$$ math — its backslashes are legitimate, scanned-around.
_MATH = re.compile(r"\$\$.+?\$\$|\$[^$]+\$", re.DOTALL)


def _flatten(node: Chunk, out: list[Chunk]) -> None:
    for c in node.children:
        out.append(c)
        _flatten(c, out)


def _limit_sections(tree: Chunk, n: int) -> Chunk:
    """Truncate to the first ``n`` top-level heading subtrees (plus any
    leading front-matter), for a small write trial before the full book."""
    kept: list[Chunk] = []
    seen = 0
    for child in tree.children:
        if child.kind == "heading":
            seen += 1
            if seen > n:
                break
        kept.append(child)
    tree.children = kept
    return tree


# --------------------------------------------------------------------------
# the writer (two-phase, DB)
# --------------------------------------------------------------------------


@dataclass
class ImportResult:
    draft_slug: str
    project_id: int
    chunks: dict[str, int]
    cites_resolved: int
    cites_stubbed: int
    cites_unresolved: list[str]
    glossary_created: int
    glossary_conflicts: list[tuple[str, str, str]]
    refs_resolved: int
    refs_unresolved: list[str]
    external_refs: int
    cites_pc: int
    cites_pa: int
    #: blocks tagged flag:issue/note:missing-citation (a cite with no backing
    #: reference in the corpus or any declared bib) + the distinct keys.
    missing_cite_blocks: int = 0
    missing_cite_keys: list[str] = field(default_factory=list)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


def _norm(s: str) -> str:
    """Lowercase, collapse whitespace, drop non-alphanumerics — for matching a
    verbatim quote against ingested paper text despite hyphenation/OCR drift."""
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower()))


def _tag_chunk(store: Any, chunk_id: int, pairs: list[tuple[str, str]]) -> None:
    """Tag a draft chunk (block-level via ``chunk_tags``) with ``(namespace,
    value)`` pairs — so e.g. ``flag:issue`` / ``note:techq`` blocks are
    directly queryable."""
    from precis.store._mappers import _upsert_tag

    with store.pool.connection() as c:
        for ns, val in pairs:
            tid = _upsert_tag(c, ns.upper(), val)  # tags.namespace must be UPPER
            c.execute(
                "INSERT INTO chunk_tags (chunk_id, tag_id, set_by) "
                "VALUES (%s, %s, 'system') ON CONFLICT (chunk_id, tag_id) DO NOTHING",
                (chunk_id, tid),
            )


_NOTE_SENTINEL = re.compile(r"⟦note:(\d+)⟧")
#: an unresolved citation marker that survived phase 2 (no backing reference
#: anywhere) — the block carrying it is flagged ``flag:issue``/``note:missing-citation``.
_DANGLING_CITE = re.compile(r"\[§([^\]]+)\]")


def _match_quote(
    store: Any, ref_id: int, quote: str, cache: dict[int, list]
) -> int | None:
    """Find the paper chunk whose text contains the verbatim quote and return
    its chunk_id (-> ``[pc<id>]``), or None. Matches a distinctive normalized
    prefix; the paper's chunks are cached per ref_id (papers are cited often)."""
    needle = _norm(quote).strip()[:50]
    if len(needle) < 15:
        return None
    chunks = cache.get(ref_id)
    if chunks is None:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
                (ref_id,),
            ).fetchall()
        chunks = [(int(cid), _norm(txt)) for cid, txt in rows]
        cache[ref_id] = chunks
    for cid, hay in chunks:
        if needle in hay:
            return cid
    return None


class _PinnedPool:
    """Make every ``pool.connection()`` yield ONE pinned connection (no
    close/commit) so a whole multi-method import runs in a single outer
    transaction — the methods' own ``self.tx()`` blocks nest as savepoints."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @contextmanager
    def connection(self, *_a: Any, **_k: Any) -> Any:
        yield self._conn  # do NOT close; the outer transaction owns commit/rollback


@contextmanager
def _atomic(store: Any) -> Any:
    """Run all enclosed store writes in one transaction: commit on clean
    exit, **roll back everything on any exception** (nothing half-written).
    Pins one connection for the duration; restores the real pool after."""
    real = store.pool
    with real.connection() as conn:
        store.pool = _PinnedPool(conn)
        try:
            with conn.transaction():
                yield conn
        finally:
            store.pool = real


def _retire_existing_draft(store: Any, slug: str) -> None:
    """Make re-import idempotent: if a draft already lives under ``slug``,
    retire it (and its owning project) and free the slug.

    The slug is an alias row in ``ref_identifiers`` (``id_kind='cite_key'``),
    so we resolve the live draft through it. Without this, ``insert_ref``'s
    ``ON CONFLICT (id_kind,id_value) DO NOTHING`` silently drops the slug on a
    re-run and leaves the *old* draft bound — i.e. a duplicate. We soft-delete
    the draft + project, drop the slug identifier and the ``draft-of`` link so
    the fresh import reclaims the slug cleanly. Runs inside the import's outer
    transaction, so a later failure rolls the retire back too."""
    existing = store.get_ref(kind="draft", id=slug)
    if existing is None:
        return
    with store.tx() as conn:
        proj = conn.execute(
            "SELECT dst_ref_id FROM links "
            "WHERE src_ref_id = %s AND relation = 'draft-of'",
            (existing.id,),
        ).fetchone()
        store.soft_delete_ref(existing.id, conn=conn)
        conn.execute(
            "DELETE FROM ref_identifiers WHERE id_kind = 'cite_key' AND id_value = %s",
            (slug,),
        )
        conn.execute(
            "DELETE FROM links WHERE src_ref_id = %s AND relation = 'draft-of'",
            (existing.id,),
        )
        if proj is not None:
            try:
                store.soft_delete_ref(int(proj[0]), conn=conn)
            except Exception:
                pass


def run_import(
    store: Any,
    root: Path,
    *,
    slug: str,
    title: str,
    project: int | None = None,
    bib_path: Path | None = None,
    create_stubs: bool = False,
    limit_sections: int | None = None,
    atomic: bool = True,
    replace: bool = True,
    set_by: str = "tex-import",
) -> ImportResult:
    """Materialise a LaTeX document as a draft (two-phase), ADR 0036 handles.

    Phase 1 creates every chunk in order with de-macroed text:
    * **citations** -> ``[pc<chunk_id>]`` (quote-matched to the exact paper
      passage) or ``[pa<ref_id>]`` (the paper) — the cite_resolver does the
      store-backed quote-match; an unresolved key stays ``[§key]``;
    * **cross-refs** are left as deferred ``[¶@label]`` and the chunk's
      ``dc<chunk_id>`` handle is recorded per ``\\label``.
    Phase 2 rewrites the deferred cross-refs to single-bracket ``[dc<id>]`` and
    re-links external-ref ``[§key]`` cites to ``[dc<id>]`` of their reference
    chunk. A ``[§key]`` that *still* survives (no corpus paper, no DOI/arXiv,
    not in any declared bib) is a citation with no backing reference anywhere:
    its block is tagged ``flag:issue``/``note:missing-citation`` so "needs a
    real reference" is directly queryable, not a silent marker. DB writes go
    through ``store``; ``atomic`` wraps them in one transaction.
    """
    full = flatten_inputs(root)
    pre = full[: full.find(r"\begin{document}")]
    macros = harvest_macros(pre)
    pmac = harvest_param_macros(pre)
    gloss = root.parent / "tex" / "glossary-entries.tex"
    acro = (
        harvest_acronyms(gloss.read_text(encoding="utf-8", errors="replace"))
        if gloss.exists()
        else {}
    )
    # Resolve the bibliography the document actually declares
    # (\bibliography/\addbibresource, master-relative) and merge every named
    # .bib. An explicit bib_path is honoured as an extra/override source. This
    # replaces the old directory-glob guess, which mis-grabbed a shared sibling
    # references.bib that lacked the keys -> citations fell through to dangling
    # [§key] markers the reader shows as undefined.
    bib_files = list(bib_paths_in(full, root.parent))
    if bib_path and bib_path not in bib_files:
        bib_files.append(bib_path)
    bib: dict[str, Any] = {}
    for bf in bib_files:
        bib.update(parse_bib(bf.read_text(encoding="utf-8", errors="replace")))
    # capture \mtechq/\mrev as ⟦note:N⟧ sentinels (-> in-context note chunks)
    body, notes = extract_annotations(document_body(full))
    tree = walk_document(body)
    if limit_sections is not None:
        tree = _limit_sections(tree, limit_sections)

    # cite keymap (key -> precis cite_key; opt-in stubs for missing-but-DOI)
    flat: list[Chunk] = []
    _flatten(tree, flat)
    # Collect the keys from the SAME macro-expanded view the emit pass sees.
    # demacro expands custom macros (e.g. methane's \deepcite{key}{page}{quote})
    # into a \cite/\mciteboxp *before* extracting cites — so reading cites from
    # raw chunk text misses them, and an unseen key never enters the keymap and
    # leaks as a dangling [§key]. A throwaway demacro whose cite_resolver just
    # records keys mirrors the emit pass exactly. Note bodies (\mtechq/\mrev)
    # can carry cites too, so collect from those as well.
    collected: set[str] = set()

    def _collect_keys(c: Any) -> str:
        collected.update(c.keys)
        return ""

    for c in flat:
        if c.text and c.kind not in ("equation", "table"):
            demacro(
                c.text,
                macros=macros,
                param_macros=pmac,
                acronyms=acro,
                cite_resolver=_collect_keys,
            )
    for nd in notes:
        demacro(
            nd.get("text", ""),
            macros=macros,
            param_macros=pmac,
            acronyms=acro,
            cite_resolver=_collect_keys,
        )
    used = sorted(collected)

    # Every DB write below runs in ONE transaction when atomic=True: a
    # failure anywhere (a stub mint, a chunk insert, a ref resolve) rolls
    # back the project + draft + all chunks + any stubs — nothing is left
    # half-written, so we can fix and re-run from a clean slate.
    out: dict[str, Any] = {"project": project}

    def _write() -> None:
        if replace:
            _retire_existing_draft(store, slug)
        km = build_keymap(store, bib, used, create_stubs=create_stubs)
        if out["project"] is None:
            from precis.dispatch import Hub
            from precis.handlers.todo import TodoHandler

            resp = TodoHandler(hub=Hub(store=store)).put(
                text=f"Import & edit: {title}",
                tags=["level:strategic"],
                meta={
                    "workspace": {
                        "path": f"imports/{slug}",
                        "brief": f"Imported from {root.name}; review flagged tables/equations.",
                    }
                },
            )
            out["project"] = _id_of(resp.body)

        draft_ref, title_chunk = store.create_draft(
            name=slug,
            title=title,
            project_ref_id=int(out["project"]),
            meta={
                "workspace": {"path": f"imports/{slug}", "format": "tex"},
                "imported_from": str(root),
            },
        )
        gres = import_glossary(store, draft_ref.id, acro)

        # citation resolver (store-backed): a quote-bearing cite anchors to the
        # specific paper chunk -> [pc<id>]; otherwise the paper -> [pa<id>]; an
        # unresolved key stays [§key] for the external-ref re-link in phase 2.
        quote_cache: dict[int, list] = {}
        pc_pa = Counter()

        def _cite_resolver(c: Any) -> str:
            parts = []
            for key in c.keys:
                rid = km.refid.get(key)
                if rid is None:
                    parts.append(f"[§{km.slug.get(key, key)}]")
                    continue
                cid = (
                    _match_quote(store, rid, c.quote, quote_cache) if c.quote else None
                )
                if cid is not None:
                    pc_pa["pc"] += 1
                    parts.append(f"[pc{cid}]")
                else:
                    pc_pa["pa"] += 1
                    parts.append(f"[pa{rid}]")
            return "".join(parts)

        # phase 1: create chunks in order, collect label → handle
        counts: Counter[str] = Counter()
        labels: dict[str, str] = {}

        def _emit_notes(note_ids: list[int], after_handle: str) -> str:
            """Materialise captured \\mtechq/\\mrev as in-context note chunks
            (paragraphs, tagged) chained after ``after_handle``. Returns the
            handle of the last note (so callers can keep placing after it)."""
            anchor = after_handle
            for nid in note_ids:
                nd = notes[nid]
                lead = (
                    "Open question"
                    if nd["type"] == "techq"
                    else f"Review finding ({nd.get('sev', '').upper()})".replace(
                        " ()", ""
                    )
                )
                ntext = f"{lead} [{nd['code']}]: " + demacro(
                    nd["text"],
                    macros=macros,
                    param_macros=pmac,
                    acronyms=acro,
                    keymap=km.slug,
                    cite_resolver=_cite_resolver,
                )
                nmade = store.add_chunks(
                    ref_id=draft_ref.id,
                    chunk_kind="paragraph",
                    text=ntext,
                    at={"after": anchor},
                )
                if not nmade:
                    continue
                anchor = nmade[0].handle
                tags = [("flag", "issue"), ("note", nd["type"])]
                if nd.get("sev"):
                    tags.append(("sev", nd["sev"]))
                _tag_chunk(store, nmade[0].chunk_id, tags)
                counts["note"] += 1
            return anchor

        # parent is threaded as (base-58 handle for store placement, dc<id>
        # handle for cross-ref emission — the ADR 0036 agent-facing address).
        def _emit(node: Chunk, parent_handle: str, parent_dc: str) -> None:
            for child in node.children:
                raw = child.text or ""
                if child.kind in ("equation", "table"):
                    text = re.sub(
                        r"\n\s*\n+", "\n", raw
                    ).strip()  # keep raw, don't re-split
                else:
                    text = demacro(
                        raw,
                        macros=macros,
                        param_macros=pmac,
                        acronyms=acro,
                        keymap=km.slug,
                        cite_resolver=_cite_resolver,
                    )
                note_ids = [int(m) for m in _NOTE_SENTINEL.findall(text)]
                if note_ids:
                    text = _NOTE_SENTINEL.sub("", text).strip()
                if not text.strip() and not child.children:
                    if note_ids:
                        _emit_notes(note_ids, parent_handle)  # sentinel-only chunk
                    else:
                        # a standalone \label cleans to empty — keep its label,
                        # anchored to the enclosing heading so \ref{sec:x} resolves.
                        for lab in labels_in(raw):
                            labels[lab] = parent_dc
                    continue
                meta = {"flag": child.meta["flag"]} if child.meta.get("flag") else None
                made = store.add_chunks(
                    ref_id=draft_ref.id,
                    chunk_kind=child.kind,
                    text=text or "(figure omitted)",
                    at={"into": parent_handle, "last": True},
                    meta=meta,
                )
                counts[child.kind] += 1
                handle = made[0].handle if made else parent_handle
                dc = made[0].dc if made else parent_dc
                for lab in labels_in(raw):
                    labels[lab] = dc
                if note_ids:
                    _emit_notes(note_ids, handle)
                _emit(child, handle, dc)

        _emit(tree, title_chunk.handle, title_chunk.dc)

        # External References: cited keys with no corpus paper and no DOI/arXiv
        # (books, patents, standards). Capture what the .bib knows as chunks in
        # a trailing section; in-text [§key] cites to them become [[dc<id>]]
        # handle cross-refs instead of dangling placeholders.
        ext_keys = sorted(k for k in km.unresolved if k in bib)
        ext_map: dict[str, str] = {}
        if ext_keys:
            sec = store.add_chunks(
                ref_id=draft_ref.id,
                chunk_kind="heading",
                text="External References (books, patents, and standards not in the paper corpus)",
                at={"into": title_chunk.handle, "last": True},
            )
            sec_handle = sec[0].handle if sec else title_chunk.handle
            for k in ext_keys:
                ref_text = (
                    demacro(
                        bib[k].human(), macros=macros, param_macros=pmac, acronyms=acro
                    )
                    or k
                )
                made = store.add_chunks(
                    ref_id=draft_ref.id,
                    chunk_kind="paragraph",
                    text=ref_text,
                    at={"into": sec_handle, "last": True},
                    meta={"bibkey": k},
                )
                if made:
                    ext_map[k] = made[0].dc
        ext_re = (
            re.compile(r"\[§(" + "|".join(re.escape(k) for k in ext_map) + r")\]")
            if ext_map
            else None
        )

        # phase 2: resolve deferred cross-refs + re-link external-ref cites
        refs_missing: list[str] = []
        refs_done = 0
        missing_cite_keys: set[str] = set()
        missing_cite_blocks = 0
        for ch in store.reading_order(draft_ref.id):
            text = ch.text or ""
            new = text
            if "[¶@" in new:
                new = resolve_deferred(new, labels=labels, unresolved=refs_missing)
            if ext_re and "[§" in new:
                new = ext_re.sub(lambda m: f"[{ext_map[m.group(1)]}]", new)
            if new != text:
                store.edit_text(ch.handle, new)
                refs_done += 1
            # Any [§key] still here is a citation with NO backing reference
            # anywhere — not in the corpus, not in any bib the document
            # declares. Flag the block (same flag:issue convention as the
            # \mtechq/\mrev notes) so "needs a real reference" is directly
            # queryable, not a silent undefined marker in the prose.
            dangling = _DANGLING_CITE.findall(new)
            if dangling:
                missing_cite_keys.update(dangling)
                missing_cite_blocks += 1
                _tag_chunk(
                    store,
                    ch.chunk_id,
                    [("flag", "issue"), ("note", "missing-citation")],
                )
        out.update(
            km=km,
            gres=gres,
            counts=counts,
            refs_done=refs_done,
            refs_missing=refs_missing,
            ext_refs=len(ext_map),
            pc=pc_pa["pc"],
            pa=pc_pa["pa"],
            missing_cite_blocks=missing_cite_blocks,
            missing_cite_keys=sorted(missing_cite_keys),
        )

    with _atomic(store) if atomic else nullcontext():
        _write()

    km, gres = out["km"], out["gres"]
    return ImportResult(
        draft_slug=slug,
        project_id=int(out["project"]),
        chunks=dict(out["counts"]),
        cites_resolved=len([k for k in used if k in km.slug]),
        cites_stubbed=len(km.stubbed),
        cites_unresolved=km.unresolved,
        glossary_created=len(gres.created),
        glossary_conflicts=gres.conflicts,
        refs_resolved=out["refs_done"],
        refs_unresolved=sorted(set(out["refs_missing"])),
        external_refs=out["ext_refs"],
        cites_pc=out["pc"],
        cites_pa=out["pa"],
        missing_cite_blocks=out["missing_cite_blocks"],
        missing_cite_keys=out["missing_cite_keys"],
    )


def _load_macros_acronyms(root: Path) -> tuple[dict, dict, dict]:
    full = flatten_inputs(root)
    preamble = full[: full.find(r"\begin{document}")]
    macros = harvest_macros(preamble)
    pmac = harvest_param_macros(preamble)
    gloss = root.parent / "tex" / "glossary-entries.tex"
    acro = (
        harvest_acronyms(gloss.read_text(encoding="utf-8", errors="replace"))
        if gloss.exists()
        else {}
    )
    return macros, pmac, acro


def dry_run(root: Path, *, limit_sections: int | None = None) -> str:
    """Build the full plan (no DB) and report structure + residual LaTeX."""
    macros, pmac, acro = _load_macros_acronyms(root)
    full = flatten_inputs(root)
    # strip editorial annotation macros before structural splitting (their
    # multi-paragraph args would otherwise be cut mid-argument).
    tree = walk_document(strip_annotations(document_body(full)))
    if limit_sections is not None:
        tree = _limit_sections(tree, limit_sections)
    chunks: list[Chunk] = []
    _flatten(tree, chunks)

    kinds = Counter(c.kind for c in chunks)
    residual: Counter[str] = Counter()
    example: dict[str, str] = {}
    flags: Counter[str] = Counter()

    for c in chunks:
        if c.meta.get("flag"):
            flags[c.meta["flag"]] += 1
        if c.kind in ("equation", "table"):
            continue  # raw LaTeX is retained here on purpose
        cleaned = demacro(c.text or "", macros=macros, param_macros=pmac, acronyms=acro)
        scan = _MATH.sub(" ", cleaned)  # don't flag legit inline-math commands
        for m in _CMD.finditer(scan):
            cmd = m.group(0)
            residual[cmd] += 1
            if cmd not in example:
                lo = max(0, m.start() - 32)
                example[cmd] = scan[lo : m.end() + 32].replace("\n", " ").strip()

    out: list[str] = []
    out.append(f"# import build dry-run — {root.name}")
    out.append("")
    out.append(f"chunks: {len(chunks)} total")
    for kind, n in kinds.most_common():
        out.append(f"  {kind}: {n}")
    out.append("")
    out.append(f"deferred flags: {dict(flags)}")
    out.append("")
    if not residual:
        out.append("## residual LaTeX: none 🎉 (every command handled)")
    else:
        out.append(
            f"## residual LaTeX commands ({len(residual)} distinct, "
            f"{sum(residual.values())} occurrences) — to deal with"
        )
        for cmd, n in residual.most_common(40):
            out.append(f"  {n:5}  {cmd:24}  e.g. …{example[cmd]}…")
    out.append("")
    out.append(
        f"residual stray braces: {sum(c.text.count('{') for c in chunks if c.kind not in ('equation', 'table'))} "
        "(in raw; de-macro strips outside math)"
    )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import writer / dry-run.")
    ap.add_argument("root", type=Path)
    ap.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write the dry-run report here (default: stdout).",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="Actually create the draft (needs --database-url, --slug, --title).",
    )
    ap.add_argument("--slug", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--bib", type=Path, default=None)
    ap.add_argument(
        "--project",
        type=int,
        default=None,
        help="Existing project todo id; omit to mint a new one.",
    )
    ap.add_argument(
        "--create-stubs",
        action="store_true",
        help="Mint stub papers for cited DOIs not yet in the corpus.",
    )
    ap.add_argument(
        "--limit-sections",
        type=int,
        default=None,
        help="Import only the first N top-level sections (a write trial).",
    )
    ap.add_argument(
        "--no-replace",
        action="store_true",
        help="Do NOT retire an existing same-slug draft first "
        "(default: replace, so re-import is idempotent).",
    )
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args(argv)

    if not args.write:
        report = dry_run(args.root, limit_sections=args.limit_sections)
        if args.report:
            args.report.write_text(report, encoding="utf-8")
            print(f"wrote {args.report}")
        else:
            print(report)
        return 0

    if not args.slug or not args.title:
        print("--write needs --slug and --title", file=sys.stderr)
        return 2
    from precis.cli._common import resolve_dsn
    from precis.store import Store

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        res = run_import(
            store,
            args.root,
            slug=args.slug,
            title=args.title,
            project=args.project,
            bib_path=args.bib,
            create_stubs=args.create_stubs,
            limit_sections=args.limit_sections,
            replace=not args.no_replace,
        )
    finally:
        store.close()
    print(f"draft '{res.draft_slug}' created under project #{res.project_id}")
    print(f"  chunks: {res.chunks}")
    print(
        f"  cites: {res.cites_resolved} resolved, {res.cites_stubbed} stubbed, "
        f"{len(res.cites_unresolved)} unresolved"
    )
    print(
        f"  glossary: {res.glossary_created} terms, {len(res.glossary_conflicts)} conflicts"
    )
    print(
        f"  cross-refs: {res.refs_resolved} resolved, {len(res.refs_unresolved)} dangling"
    )
    print(
        f"  citation handles: {res.cites_pc} paragraph-precise [pc<id>], {res.cites_pa} paper [pa<id>]"
    )
    print(
        f"  notes: {res.chunks.get('note', 0)} tech-questions/review-findings → tagged note chunks (flag:issue)"
    )
    print(
        f"  external refs: {res.external_refs} (books/patents/standards → trailing section)"
    )
    print(
        f"  missing citations: {res.missing_cite_blocks} blocks flagged "
        f"(flag:issue/note:missing-citation), {len(res.missing_cite_keys)} keys "
        f"with no backing reference: {res.missing_cite_keys[:8]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
