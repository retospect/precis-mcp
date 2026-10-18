"""sandbox_run ``mode:script`` — ``code_ref`` resolution and ``inputs`` staging.

Both read corpus refs executor-side (trusted, has the store and
``PRECIS_ROOT``) and write plain files the offline container reads.
Called by ``sandbox_run.validate_submit`` (so a bad ref refuses at
``put`` time) and again by ``claude_docker._launch_script`` (a
dispatch-minted job never passed through ``put``, and the ref may have
changed since).

``code_ref`` rules (decided 2026-10-18, docs/backlog/sandbox-python-script.md):

* a ``plaintext`` ref is its whole body, staged as ``/work/main.py``;
* a ``markdown`` ref is its single ```` ```python ```` fenced block; zero
  or several blocks refuse unless ``params.block`` (1-based) picks one;
* a ``folder`` ref (a harvested build tree) is staged under
  ``/work/code/`` and runs ``main.py``, or ``params.entry`` when the
  tree has no ``main.py``.

``inputs`` entries are ref targets (``kind:identifier`` or a handle such
as ``st123``), staged into a host dir mounted read-only at ``/work/in``:
a ``plaintext``/``markdown`` ref as its file (``in/<file name>``), a
``structure`` as ``in/<handle>.extxyz`` plus ``in/<handle>.cif`` when
ASE is installed, a ``folder`` as its tree under ``in/<handle>/``.
``in/manifest.json`` maps each entry to the paths it produced.

Every refusal is a :class:`ScriptRefError` whose text ends in a
``next:`` step.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

#: ``inputs`` caps: entries per job and staged bytes per job.
MAX_INPUTS = 32
MAX_INPUT_BYTES = 256 * 1024 * 1024

#: Kinds ``code_ref`` / ``inputs`` accept.
CODE_REF_KINDS = ("plaintext", "markdown", "folder")
INPUT_KINDS = ("plaintext", "markdown", "structure", "folder")

#: A ``params.entry`` value: a relative ``.py`` path of plain segments.
#: It lands inside an ``sh -c`` string, so the alphabet stays closed.
_ENTRY_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*\.py\Z"
)

_PY_FENCE_OPEN = re.compile(r"^```python\b[^\n`]*$")
_FENCE_CLOSE = re.compile(r"^```\s*$")


class ScriptRefError(ValueError):
    """A ``code_ref`` / ``inputs`` refusal; the text names a ``next:`` step."""


@dataclass(frozen=True, slots=True)
class ResolvedCode:
    """What :func:`resolve_code_ref` found.

    Exactly one of ``source`` (a single-file script) and ``folder_id``
    (a tree staged under ``/work/code``) is set; ``entry`` is the path
    the container runs, relative to ``/work``.
    """

    entry: str
    source: str | None = None
    folder_id: int | None = None


# ── pure: submit-time shape checks ──────────────────────────────────


def entry_rejection(entry: Any) -> str | None:
    """Shape check for ``params.entry``."""
    if entry is None:
        return None
    if not isinstance(entry, str) or not _ENTRY_RE.match(entry) or ".." in entry:
        return (
            f"sandbox_run: params.entry {entry!r} is not a relative .py path "
            "(letters, digits, '_', '.', '-', '/' segments; no '..'); next: "
            "pass args={'entry': 'pkg/run.py'}"
        )
    return None


def block_rejection(block: Any) -> str | None:
    """Shape check for ``params.block`` (1-based)."""
    if block is None:
        return None
    if not isinstance(block, int) or isinstance(block, bool) or block < 1:
        return (
            "sandbox_run: params.block must be a positive integer (1 is the "
            "first ```python block); next: pass args={'block': 1}"
        )
    return None


def inputs_rejection(inputs: Any) -> str | None:
    """Shape check for ``params.inputs`` (no store needed)."""
    if inputs is None:
        return None
    if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
        return (
            "sandbox_run: params.inputs must be a list of ref targets; next: "
            "pass e.g. ['structure:123', 'markdown:notes--data']"
        )
    if len(inputs) > MAX_INPUTS:
        return (
            f"sandbox_run: params.inputs has {len(inputs)} entries (cap "
            f"{MAX_INPUTS}); next: bundle related files into one folder ref"
        )
    if len(set(inputs)) != len(inputs):
        return "sandbox_run: params.inputs lists a ref twice; next: drop the duplicate"
    for target in inputs:
        if not target.strip() or "~" in target:
            return (
                f"sandbox_run: params.inputs entry {target!r} is not a whole-ref "
                "target (block selectors are not staged); next: name the ref as "
                "'kind:identifier' or a handle"
            )
    return None


def extract_python_block(body: str, *, block: int | None, label: str) -> str:
    """The ```` ```python ```` block of a markdown body.

    One block: that block. Several or none: :class:`ScriptRefError`
    unless ``block`` (1-based) names one that exists.
    """
    blocks: list[str] = []
    current: list[str] | None = None
    for line in body.splitlines():
        if current is None:
            if _PY_FENCE_OPEN.match(line.strip()):
                current = []
        elif _FENCE_CLOSE.match(line.strip()):
            blocks.append("\n".join(current) + "\n")
            current = None
        else:
            current.append(line)
    if block is not None:
        if block > len(blocks):
            raise ScriptRefError(
                f"sandbox_run: code_ref {label} has {len(blocks)} ```python "
                f"block(s), so block {block} does not exist; next: pass "
                f"args={{'block': N}} with N between 1 and {len(blocks)}"
                if blocks
                else f"sandbox_run: code_ref {label} has no ```python block; "
                "next: add one, or pass the script inline as code="
            )
        return blocks[block - 1]
    if len(blocks) == 1:
        return blocks[0]
    if not blocks:
        raise ScriptRefError(
            f"sandbox_run: code_ref {label} has no ```python fenced block; "
            "next: add one, or pass the script inline as code="
        )
    raise ScriptRefError(
        f"sandbox_run: code_ref {label} has {len(blocks)} ```python blocks; "
        f"next: pass args={{'block': N}} (1..{len(blocks)}) to pick one"
    )


# ── impure: ref lookup ──────────────────────────────────────────────


def _resolve_target(store: Store, target: str, *, allowed: tuple[str, ...]) -> Any:
    """``target`` → the live ``Ref`` row, or :class:`ScriptRefError`."""
    from precis.errors import PrecisError
    from precis.utils.link_target import parse_link_target

    try:
        resolved = parse_link_target(target, store=store)
    except PrecisError as exc:
        raise ScriptRefError(
            f"sandbox_run: ref {target!r} does not resolve ({exc}); next: name "
            "it as 'kind:identifier' or a handle"
        ) from exc
    if resolved.kind not in allowed or resolved.pos is not None:
        raise ScriptRefError(
            f"sandbox_run: ref {target!r} is a {resolved.kind} ref; only "
            f"{', '.join(allowed)} refs are accepted here; next: export it to "
            "one of those kinds first"
        )
    ref = store.get_ref(kind=resolved.kind, id=resolved.ref_id)
    if ref is None:  # pragma: no cover - parse_link_target already checked
        raise ScriptRefError(
            f"sandbox_run: ref {target!r} not found; next: check the id"
        )
    return ref


def _precis_root() -> Path | None:
    from precis.config import load_config

    raw = load_config().root
    return Path(raw).expanduser().resolve() if raw else None


def _file_path(store: Store, ref: Any, *, root: Path | None) -> Path:
    """The on-disk file behind a ``plaintext``/``markdown`` ref.

    Mirrors the handlers' ``_resolve_path`` for an ingested ref under the
    workspace root (stored ``meta.ext``, else the kind's default
    extension) without importing ``precis.handlers`` — workers sit below
    the handler layer (import contract "workers never import handlers").
    """
    from precis.utils.md_parse import is_valid_file_slug, path_from_file_slug

    effective_root = root if root is not None else _precis_root()
    if effective_root is None or not ref.slug:
        raise ScriptRefError(
            f"sandbox_run: {ref.kind} ref {ref.slug or ref.id} has no file "
            "here (PRECIS_ROOT is unset on the executor host); next: run on "
            "a sandbox host with PRECIS_ROOT, or pass the content inline"
        )
    slug = str(ref.slug)
    default_ext = ".md" if ref.kind == "markdown" else ".txt"
    ext = (ref.meta or {}).get("ext") or default_ext
    base = effective_root.resolve()
    path = (base / path_from_file_slug(slug, ext=ext)).resolve()
    if not is_valid_file_slug(slug) or not path.is_relative_to(base):
        raise ScriptRefError(
            f"sandbox_run: {ref.kind} ref {slug} has no readable file "
            f"(not a workspace file slug); next: check "
            f"get(kind='{ref.kind}', id='{slug}')"
        )
    if not path.is_file():
        raise ScriptRefError(
            f"sandbox_run: {ref.kind} ref {ref.slug} has no file on disk; "
            f"next: check get(kind='{ref.kind}', id='{ref.slug}')"
        )
    return path


def resolve_code_ref(
    store: Store,
    code_ref: str,
    *,
    block: int | None = None,
    entry: str | None = None,
    root: Path | None = None,
) -> ResolvedCode:
    """Apply the ``code_ref`` rules (module docstring)."""
    ref = _resolve_target(store, code_ref, allowed=CODE_REF_KINDS)
    if ref.kind != "markdown" and block is not None:
        raise ScriptRefError(
            f"sandbox_run: params.block applies to a markdown code_ref, not "
            f"{ref.kind}; next: drop block"
        )
    if ref.kind != "folder" and entry is not None:
        raise ScriptRefError(
            f"sandbox_run: params.entry applies to a folder code_ref, not "
            f"{ref.kind}; next: drop entry"
        )
    if ref.kind == "folder":
        names = _folder_paths(store, int(ref.id))
        chosen = entry or "main.py"
        if chosen not in names:
            if entry is None:
                raise ScriptRefError(
                    f"sandbox_run: folder code_ref {code_ref} has no main.py; "
                    "next: pass args={'entry': '<file>'} naming its entry "
                    f"script (it has {_preview(names)})"
                )
            raise ScriptRefError(
                f"sandbox_run: folder code_ref {code_ref} has no {entry}; "
                f"next: pass args={{'entry': '<file>'}} with one of "
                f"{_preview(names)}"
            )
        return ResolvedCode(entry=f"code/{chosen}", folder_id=int(ref.id))
    path = _file_path(store, ref, root=root)
    body = path.read_text(encoding="utf-8", errors="replace")
    if ref.kind == "markdown":
        body = extract_python_block(body, block=block, label=code_ref)
    if not body.strip():
        raise ScriptRefError(
            f"sandbox_run: code_ref {code_ref} is empty; next: put the script in it"
        )
    return ResolvedCode(entry="main.py", source=body)


def _preview(names: list[str], n: int = 8) -> str:
    py = [x for x in names if x.endswith(".py")] or names
    shown = ", ".join(py[:n])
    return f"{shown}{', …' if len(py) > n else ''}" if py else "no files"


def _folder_paths(store: Store, folder_id: int) -> list[str]:
    """Relative paths a harvested folder restores (its children's
    ``meta.harvest_orig_path``)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->>'harvest_orig_path' FROM refs "
            " WHERE parent_id = %s AND kind = 'plaintext' AND retired_at IS NULL "
            "   AND meta ? 'harvest_orig_path'",
            (folder_id,),
        ).fetchall()
    return sorted(str(r[0]) for r in rows if r[0])


# ── impure: staging ─────────────────────────────────────────────────


def _stage_folder(store: Store, folder_id: int, dest: Path) -> None:
    from precis.workers.executors import _sandbox_harvest

    dest.mkdir(parents=True, exist_ok=True)
    try:
        _sandbox_harvest.stage_run_artifact(store, folder_id=folder_id, dest_dir=dest)
    except ValueError as exc:
        raise ScriptRefError(
            f"sandbox_run: folder:{folder_id} could not be staged ({exc}); "
            "next: use a folder a sandbox_run harvest produced"
        ) from exc


def stage_code(store: Store, resolved: ResolvedCode, work_dir: Path) -> None:
    """Write the resolved script under ``work_dir`` (``main.py`` or
    ``code/<tree>``)."""
    if resolved.folder_id is not None:
        _stage_folder(store, resolved.folder_id, work_dir / "code")
        return
    (work_dir / "main.py").write_text(str(resolved.source), encoding="utf-8")


def _handle(ref: Any) -> str:
    from precis.utils import handle_registry

    return handle_registry.try_format(ref.kind, int(ref.id)) or f"{ref.kind}-{ref.id}"


def _tree_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def check_input(store: Store, target: str) -> None:
    """Refuse an ``inputs`` entry that would not stage (no files written)."""
    ref = _resolve_target(store, target, allowed=INPUT_KINDS)
    if ref.kind in ("plaintext", "markdown"):
        _file_path(store, ref, root=None)


def stage_inputs(
    store: Store, inputs: list[str], dest: Path, *, root: Path | None = None
) -> dict[str, list[str]]:
    """Stage every ``inputs`` entry into ``dest`` (module docstring) and
    write ``dest/manifest.json``. Returns the manifest: entry → paths
    relative to ``/work/in``. Over :data:`MAX_INPUT_BYTES` refuses."""
    from precis.structure import export

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    manifest: dict[str, list[str]] = {}
    total = 0
    for target in inputs:
        ref = _resolve_target(store, target, allowed=INPUT_KINDS)
        handle = _handle(ref)
        written: list[Path] = []
        if ref.kind in ("plaintext", "markdown"):
            src = _file_path(store, ref, root=root)
            if total + src.stat().st_size > MAX_INPUT_BYTES:
                raise ScriptRefError(
                    f"sandbox_run: inputs exceed {MAX_INPUT_BYTES} bytes at "
                    f"{target!r}; next: stage fewer or smaller refs"
                )
            out = dest / src.name
            if out.exists():
                out = dest / f"{handle}-{src.name}"
            shutil.copyfile(src, out)
            written.append(out)
        elif ref.kind == "structure":
            scene, _handles = store.structure_load(int(ref.id))
            out = dest / f"{handle}.extxyz"
            out.write_text(export.to_extxyz(scene), encoding="utf-8")
            written.append(out)
            if export.ase_available():
                cif = dest / f"{handle}.cif"
                cif.write_text(export.to_cif(scene), encoding="utf-8")
                written.append(cif)
        else:  # folder
            out = dest / handle
            _stage_folder(store, int(ref.id), out)
            written.append(out)
        total += sum(_tree_bytes(p) for p in written)
        if total > MAX_INPUT_BYTES:
            raise ScriptRefError(
                f"sandbox_run: inputs exceed {MAX_INPUT_BYTES} bytes at "
                f"{target!r}; next: stage fewer or smaller refs"
            )
        manifest[target] = [str(PurePosixPath(p.relative_to(dest))) for p in written]
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


__all__ = [
    "CODE_REF_KINDS",
    "INPUT_KINDS",
    "MAX_INPUTS",
    "MAX_INPUT_BYTES",
    "ResolvedCode",
    "ScriptRefError",
    "block_rejection",
    "check_input",
    "entry_rejection",
    "extract_python_block",
    "inputs_rejection",
    "resolve_code_ref",
    "stage_code",
    "stage_inputs",
]
