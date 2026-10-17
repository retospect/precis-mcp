"""Background runner for the slow draft downloads (``.docx``, compiled PDF).

``GET /drafts/<id>/export.docx`` and ``GET /drafts/<id>/pdf`` build the
whole file inside the request, which on a real draft takes tens of
seconds with no feedback (gr493192). This module runs the *same* route
handler in a worker thread, records its progress, and holds the finished
response so the page can poll ``…/export/status/<token>`` (progress bar)
and then fetch ``…/export/download/<token>``.

Why re-run the route rather than a parallel builder: the route owns the
export gates (retraction block, figure clearance, overrides, sources
bundle, filename). A gated or failed export comes back as a non-file
response; the job reports ``blocked`` and the page navigates to the plain
GET URL, which renders that same page.

State is per process (``precis web`` is one uvicorn process) and in
memory: a restart drops running jobs, the page shows the error and the
user presses the button again. Finished entries expire after
:data:`_TTL_S`.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, Response

from precis.budget.work_class import WorkClass, work_class_scope
from precis.export import progress

log = logging.getLogger(__name__)

_TTL_S = 3600.0
_MAX_JOBS = 64

Handler = Callable[[Request, str], Coroutine[Any, Any, Response]]


@dataclass
class ExportJob:
    token: str
    ident: str
    fmt: str
    key: str
    state: str = "running"  # running | done | blocked | error
    stage: str = "starting"
    done: int | None = None
    total: int | None = None
    error: str = ""
    response: Response | None = None
    created: float = field(default_factory=time.monotonic)
    finished: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        pct: int | None = None
        if self.state == "done":
            pct = 100
        elif self.done is not None and self.total:
            pct = min(99, int(100 * self.done / self.total))
        return {
            "state": self.state,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "pct": pct,
            "error": self.error,
            "elapsed_s": round((self.finished or time.monotonic()) - self.created, 1),
        }


_LOCK = threading.Lock()
_JOBS: dict[str, ExportJob] = {}


def _purge_locked(now: float) -> None:
    for tok in [
        t
        for t, j in _JOBS.items()
        if j.state != "running" and now - j.finished > _TTL_S
    ]:
        _drop_locked(tok)
    while len(_JOBS) > _MAX_JOBS:
        done = [j for j in _JOBS.values() if j.state != "running"]
        if not done:
            break
        _drop_locked(min(done, key=lambda j: j.created).token)


def _drop_locked(token: str) -> None:
    """Forget a job and delete the scratch dir the docx route made for it."""
    job = _JOBS.pop(token)
    path = getattr(job.response, "path", None)
    if path is not None:
        work = Path(path).parent
        if work.name.startswith("precis-docx-"):
            shutil.rmtree(work, ignore_errors=True)


def get(token: str, ident: str) -> ExportJob | None:
    with _LOCK:
        job = _JOBS.get(token)
    return job if job is not None and job.ident == ident else None


def start(
    request: Request, ident: str, fmt: str, handler: Handler, query: bytes
) -> ExportJob:
    """Run ``handler`` (a GET export route) in a thread against ``query``.

    A running job for the same draft + format + query is reused, so a
    double click or a second tab joins the build instead of doubling it."""
    key = f"{fmt}:{query.decode('latin-1')}"
    now = time.monotonic()
    with _LOCK:
        _purge_locked(now)
        for j in _JOBS.values():
            if j.ident == ident and j.key == key and j.state == "running":
                return j
        job = ExportJob(token=secrets.token_urlsafe(12), ident=ident, fmt=fmt, key=key)
        _JOBS[job.token] = job
    sub = Request({**request.scope, "query_string": query, "method": "GET"})

    def on_progress(stage: str, done: int | None, total: int | None) -> None:
        job.stage, job.done, job.total = stage, done, total

    def run() -> None:
        try:
            with progress.bind(on_progress):
                resp = asyncio.run(handler(sub, ident))
            job.response = resp
            job.finished = time.monotonic()
            job.state = "done" if isinstance(resp, FileResponse) else "blocked"
        except Exception as exc:
            log.warning("export job %s failed", job.token, exc_info=True)
            job.error = f"{type(exc).__name__}: {exc}"[:300]
            job.finished = time.monotonic()
            job.state = "error"

    def run_interactive() -> None:
        # A bare thread starts with an empty context; the request ran under
        # InteractiveWorkClassMiddleware, so keep that class for the build.
        with work_class_scope(WorkClass.INTERACTIVE):
            run()

    threading.Thread(
        target=run_interactive, name=f"export-{fmt}-{ident}", daemon=True
    ).start()
    return job
