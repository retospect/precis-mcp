# Windows CI residuals after the skipif pass

(1) Watch `tests/test_render_sandbox.py::test_no_output_is_reported` — a real
timing flake deliberately NOT skipped so it isn't masked; if it becomes the
lone red, fix the flake, don't skip. (2) The 27 skipif(win32) tests are
POSIX-only by harness, not product — the underlying behaviors aren't
Windows-tested at all; revisit only if Windows becomes a deployed runtime.
(3) From the architecture review: the Windows O_DIRECTORY + Python 3.12
urllib circular-import fixes belong here too.
