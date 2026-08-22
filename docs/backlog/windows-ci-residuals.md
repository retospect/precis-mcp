# Windows CI residuals after the skipif pass

(1) Watch `tests/test_render_sandbox.py::test_no_output_is_reported` — a real
timing flake deliberately NOT skipped so it isn't masked; if it becomes the
lone red, fix the flake, don't skip. (2) The 27 skipif(win32) tests are
POSIX-only by harness, not product — the underlying behaviors aren't
Windows-tested at all; revisit only if Windows becomes a deployed runtime.
(3) From the architecture review: the Windows O_DIRECTORY + Python 3.12
urllib circular-import fixes belong here too.

(4) **`subprocess(..., text=True)` still decodes by locale** — the other half
of the encoding class a5fd1f02 closed. That ship named `encoding="utf-8"` at
all 388 file-IO sites and guards them (ruff `PLW1514` +
`tests/test_text_io_encoding.py`), but ~64 `text=True` /
`universal_newlines=True` call sites in `src/` + `scripts/` decode the child's
stdout with `locale.getpreferredencoding()`, i.e. cp1252 on the Windows leg.
No ruff rule covers it and the AST guard deliberately doesn't either — unlike
file IO, some of these read tool output whose encoding isn't ours to assume,
so it needs per-site judgment rather than a sweep. Not currently red: the
affected paths are container/cluster (Linux) only. Fix by passing
`encoding="utf-8"` per site; extend the AST guard once the judgment calls are
made.
