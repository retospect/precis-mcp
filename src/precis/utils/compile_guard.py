"""Layer-3 compile guard — verifies the LaTeX workspace compiles.

Wired into ``TodoHandler.tag`` (alongside the artifact guardrail) so a
worker-driven ``STATUS:done`` on a leaf in a ``format='tex'``
workspace is rejected when ``latexmk`` reports errors. The leaf's
re-tick sees the errors and produces a fix; cascade never declares
victory on a broken paper.

Scope contract:

* Only runs on **root-level** done (when the parent has no live
  child todos and is about to mark itself done). Intermediate leaves
  marking sub-sections done would race on shared files and trigger
  spurious failures.
* Holds the workspace advisory lock through compile so concurrent
  ``put`` calls don't change the on-disk state during ``latexmk``.
* Regenerates ``refs.bib`` from citation refs before compiling.
* Time-capped at ``PRECIS_LATEXMK_TIMEOUT_S`` (default 120s).
* On success: no-op, ``STATUS:done`` proceeds.
* On failure: ``BadInput`` with the last 30 lines of the build log
  so the LLM's next tick prompt carries the actual error.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from precis.errors import BadInput

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


def check_workspace_compiles(
    store: Store,
    ref_id: int,
    add: list[str] | None,
    *,
    precis_root: Path | None = None,
) -> None:
    """Run ``latexmk`` if this is a workspace-root ``STATUS:done`` attempt.

    No-op when:
    * the tag operation isn't ``STATUS:done``;
    * the ref isn't a strategic-root todo with a tex workspace;
    * there are live (open) child todos under it (it's not the root
      doing its final stitch — intermediate done's would race);
    * latexmk isn't installed (we log + skip rather than block the
      cascade on a tooling gap).
    """
    if not add or "STATUS:done" not in add:
        return

    workspace = _load_workspace(store, ref_id)
    if workspace is None or workspace.format != "tex":
        return
    if _has_live_child_todos(store, ref_id):
        # Mid-cascade — leaves stitching not yet complete.
        return
    if not _have_latexmk():
        log.warning(
            "compile_guard: latexmk not on PATH; skipping STATUS:done "
            "verification for ref #%d (install mactex on the agent host)",
            ref_id,
        )
        return
    precis_root = precis_root or _resolve_precis_root()
    if precis_root is None:
        log.warning("compile_guard: PRECIS_ROOT unset; skipping verify")
        return
    ws_root = workspace.absolute_root(precis_root)
    if not ws_root.exists():
        return

    # Regenerate refs.bib before compile so the latest citation set
    # is on disk. Cheap (small bib, plain Python).
    project_tag = f"project:{workspace.path.rstrip('/').split('/')[-1]}"
    try:
        from precis.utils.bib_gen import write_workspace_bib

        n_entries = write_workspace_bib(
            store, workspace_root=ws_root, project_tag=project_tag
        )
        log.info(
            "compile_guard: refs.bib regenerated with %d entries for %s",
            n_entries,
            project_tag,
        )
    except Exception:
        log.exception(
            "compile_guard: refs.bib regen failed for ref #%d", ref_id
        )

    timeout_s = int(os.environ.get("PRECIS_LATEXMK_TIMEOUT_S", "120"))
    cmd = [
        "latexmk",
        "-pdf",
        "-interaction=nonstopmode",
        "-halt-on-error",
        workspace.entrypoint,
    ]
    log.info(
        "compile_guard: running latexmk for ref #%d in %s (timeout=%ds)",
        ref_id,
        ws_root,
        timeout_s,
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=ws_root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BadInput(
            f"STATUS:done blocked on todo id={ref_id}: latexmk timeout "
            f"after {timeout_s}s",
            next=(
                "the build is taking too long — either the project grew "
                "huge, or there's a loop in \\input{}. Either work on "
                "reducing scope, or yield via ask-user: with the error."
            ),
        ) from None
    if proc.returncode == 0:
        log.info("compile_guard: latexmk PASSED for ref #%d", ref_id)
        return
    # Failure — surface the last 30 log lines (or stderr if no log).
    log_path = ws_root / (Path(workspace.entrypoint).stem + ".log")
    excerpt = ""
    if log_path.exists():
        try:
            log_text = log_path.read_text(errors="replace")
            tail = log_text.splitlines()[-30:]
            excerpt = "\n".join(tail)
        except OSError:
            pass
    if not excerpt:
        excerpt = (proc.stderr or proc.stdout or "")[-2000:]
    raise BadInput(
        f"STATUS:done blocked on todo id={ref_id}: latexmk failed "
        f"(exit {proc.returncode})\n\n--- last log lines ---\n{excerpt}",
        next=(
            "fix the LaTeX error reported above and either: (a) put(kind='tex', "
            "name='<file>', text='<corrected>') for a content fix, or "
            "(b) edit(kind='tex', id='tex--<file>~<block>', ...) for a block-"
            "level patch. Then re-attempt STATUS:done."
        ),
    )


# ── helpers ────────────────────────────────────────────────────────


def _load_workspace(store: Store, ref_id: int):
    """Return the Workspace stored on the ref's meta, or None."""
    from precis.utils.workspace import Workspace

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    return Workspace.from_meta(row[0])


def _has_live_child_todos(store: Store, ref_id: int) -> bool:
    """True when this ref still has open (non-done) child todos."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM refs c
                 WHERE c.parent_id = %s
                   AND c.kind = 'todo'
                   AND c.deleted_at IS NULL
                   AND COALESCE(
                         (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                           WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                         'open'
                       ) NOT IN ('done', 'won''t-do')
            )
            """,
            (ref_id,),
        ).fetchone()
    return bool(row and row[0])


def _have_latexmk() -> bool:
    """Probe for latexmk on PATH."""
    import shutil

    return shutil.which("latexmk") is not None


def _resolve_precis_root() -> Path | None:
    """Read PRECIS_ROOT from env (same env the MCP server uses)."""
    raw = os.environ.get("PRECIS_ROOT")
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.exists() else None


__all__ = ["check_workspace_compiles"]
