"""``precis sim ingest`` — project a sim's manifest outputs into the corpus.

Slice 1 of ``docs/proposals/sim-harness.md`` (In-scope item 3, AC #3, the
"Ingest kinds — DECIDED" entry). Two steps, deliberately split:

1. **Project** — copy each manifest ``outputs:`` file that's prose/CSV
   into ``PRECIS_ROOT/sim/<slug>/`` (findings ``.md``/``.markdown`` kept
   as-is; ``.csv`` renamed to ``.txt`` — ``PlaintextHandler``'s extension
   set omits ``.csv``, content is preserved and searchable either way).
   Binary plots (``.png``/``.vti``/``.vtu``) are skipped — no
   binary-blob ingest path exists yet (deferred to the ``folder``-harvest
   slice).
2. **Ingest** — drive the same prose-ingest walker ``precis jobs ingest``
   uses (``cli/ingest.py:_ingest_one_kind``): construct the handler and
   call ``handler._ensure_ingested(slug, force=...)`` directly. This is
   the load-bearing choice — **not** the public, create-only ``put()``,
   which raises on a second call. ``_ensure_ingested``'s mtime/sha256
   content gate is what makes a second run over unchanged files a
   true no-op (AC #3).

The producing git SHA (``git rev-parse HEAD`` in the sim repo) is
recorded on each ref's ``meta`` via ``store.stamp_ref_meta`` for
provenance; a non-git sim directory is tolerated (the SHA is just
omitted, never a crash).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from precis.dispatch import Hub
from precis.handlers.markdown import MarkdownHandler
from precis.handlers.plaintext import PlaintextHandler
from precis.sim.manifest import SimManifest
from precis.sim.registry import SimEntry
from precis.store import Store
from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

log = logging.getLogger(__name__)

_MARKDOWN_EXTS = (".md", ".markdown")
_CSV_EXTS = (".csv",)
_PLAINTEXT_PASSTHROUGH_EXTS = (".txt", ".log")
_SKIPPED_BINARY_EXTS = (".png", ".vti", ".vtu")


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """Tally + human-readable log lines for one ``precis sim ingest`` run."""

    ingested: int = 0
    skipped: int = 0
    failed: int = 0
    messages: tuple[str, ...] = field(default_factory=tuple)


def _git_head_sha(repo_path: Path) -> str | None:
    """Best-effort ``git rev-parse HEAD`` in *repo_path*.

    Returns ``None`` (never raises) when *repo_path* isn't a git repo,
    git isn't on PATH, or the command otherwise fails — provenance is
    a nice-to-have, not a hard requirement of ingest succeeding.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _resolve_outputs(entry: SimEntry, manifest: SimManifest) -> list[Path]:
    """Expand ``manifest.outputs`` (literal paths or globs) against ``entry.path``.

    Order-preserving, de-duplicated. A pattern that matches nothing is
    silently dropped here — the caller reports on the aggregate.
    """
    matched: list[Path] = []
    seen: set[Path] = set()
    for pattern in manifest.outputs:
        for candidate in sorted(entry.path.glob(pattern)):
            if candidate.is_file() and candidate not in seen:
                seen.add(candidate)
                matched.append(candidate)
    return matched


def _slug_for(rel: str, *, extensions: tuple[str, ...]) -> str | None:
    """Derive the file-kind ref slug for *rel* (relative to PRECIS_ROOT).

    Mirrors ``cli/ingest.py:_ingest_one_kind``'s ``slug_for`` — strip a
    known extension, then encode via the shared ``md_parse`` helpers.
    """
    base = rel
    for ext in extensions:
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    slug = file_slug_from_path(base)
    return slug if is_valid_file_slug(slug) else None


def ingest_sim(
    *,
    slug: str,
    entry: SimEntry,
    manifest: SimManifest,
    root: Path,
    hub: Hub,
    store: Store,
    force: bool = False,
) -> IngestOutcome:
    """Project + ingest one sim's manifest ``outputs:`` into the corpus.

    ``root`` is ``PRECIS_ROOT``; files land under ``root/sim/<slug>/``.
    Returns an :class:`IngestOutcome` tally; never raises for a
    per-output problem (unmatched pattern, invalid slug, skipped binary)
    — those are folded into ``skipped``/``failed`` + ``messages`` so one
    bad output doesn't abort the rest.
    """
    sim_dir = root / "sim" / slug
    sim_dir.mkdir(parents=True, exist_ok=True)

    sha = _git_head_sha(entry.path)
    meta_updates: dict[str, object] = {"sim_slug": slug}
    if sha:
        meta_updates["sim_git_sha"] = sha

    ingested = skipped = failed = 0
    messages: list[str] = []

    outputs = _resolve_outputs(entry, manifest)
    if not outputs:
        messages.append(f"no manifest outputs matched under {entry.path}")

    md_handler: MarkdownHandler | None = None
    txt_handler: PlaintextHandler | None = None

    for src in outputs:
        ext = src.suffix.lower()
        if ext in _SKIPPED_BINARY_EXTS:
            skipped += 1
            messages.append(
                f"skip  {src.name}  - binary plot, deferred to folder-harvest slice"
            )
            continue

        if ext in _MARKDOWN_EXTS:
            kind = "markdown"
            handler_extensions = _MARKDOWN_EXTS
            dest_name = src.name
        elif ext in _CSV_EXTS:
            kind = "plaintext"
            handler_extensions = _PLAINTEXT_PASSTHROUGH_EXTS
            dest_name = src.stem + ".txt"
        elif ext in _PLAINTEXT_PASSTHROUGH_EXTS:
            kind = "plaintext"
            handler_extensions = _PLAINTEXT_PASSTHROUGH_EXTS
            dest_name = src.name
        else:
            skipped += 1
            messages.append(f"skip  {src.name}  - unrecognized extension {ext!r}")
            continue

        # Preserve the source's subpath under the sim repo, so two same-named
        # outputs in different subdirs (case1/findings.md, case2/findings.md)
        # get distinct destination slugs instead of clobbering each other.
        rel_sub = src.relative_to(entry.path)
        dest = sim_dir / rel_sub.parent / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        rel = str(dest.relative_to(root))

        file_slug = _slug_for(rel, extensions=handler_extensions)
        if file_slug is None:
            failed += 1
            messages.append(f"fail  {rel}  - invalid slug for path")
            continue

        if kind == "markdown":
            if md_handler is None:
                md_handler = MarkdownHandler(hub=hub, root=root)
            handler: MarkdownHandler | PlaintextHandler = md_handler
        else:
            if txt_handler is None:
                txt_handler = PlaintextHandler(hub=hub, root=root)
            handler = txt_handler

        ref_before = store.get_ref(kind=kind, id=file_slug)
        ref = handler._ensure_ingested(file_slug, force=force)
        if ref is None:
            failed += 1
            messages.append(f"fail  {rel}  - ingest returned None")
            continue

        store.stamp_ref_meta(ref.id, meta_updates)

        if ref_before is None:
            ingested += 1
            messages.append(f"ok    [{kind:<9}] {file_slug}")
        else:
            before_sha = (ref_before.meta or {}).get("sha256")
            after_sha = (ref.meta or {}).get("sha256")
            if force or before_sha != after_sha:
                ingested += 1
                messages.append(f"upd   [{kind:<9}] {file_slug}")
            else:
                skipped += 1
                messages.append(f"noop  [{kind:<9}] {file_slug}  (unchanged)")

    return IngestOutcome(
        ingested=ingested, skipped=skipped, failed=failed, messages=tuple(messages)
    )


__all__ = ["IngestOutcome", "ingest_sim"]
