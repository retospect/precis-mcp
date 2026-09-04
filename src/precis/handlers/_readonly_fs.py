"""Shared EROFS→``Unsupported`` translation for file-write verbs (gr311325).

Any kind whose write path touches a real filesystem — plaintext/markdown/
tex's mkstemp+``os.replace``, python's ``atomic_write`` — can have its root
mounted read-only in some deployments (e.g. a dev-stdio MCP mounting the
workspace ``:ro``). A raw ``OSError`` from that mount used to surface as an
opaque ``[error:Internal] ... OSError``; :func:`translate_readonly_fs` turns
exactly the EROFS case into a typed :class:`~precis.errors.Unsupported` with
actionable guidance, while any other ``OSError`` (disk full, a single-file
permission problem, …) is a real problem and re-raises unchanged.

Extracted from :mod:`precis.handlers.plaintext` (the original gr311325 fix,
covering plaintext/markdown/tex via inheritance) so :mod:`precis.handlers.python`
— a ``Handler``-direct kind with its own ``mkstemp``+``os.replace`` write path
in ``_python_write.atomic_write`` — can wrap its write verbs the same way
without duplicating the errno/message logic (gr311350).
"""

from __future__ import annotations

import errno
import functools
from collections.abc import Callable
from typing import Any

from precis.errors import Unsupported
from precis.response import Response

READONLY_FS_HINT = (
    "workspace is mounted read-only — file writes are disabled in this deployment"
)


def is_readonly_fs_error(exc: OSError) -> bool:
    """True for an EROFS (or equivalently-worded) OS-level write failure.

    A root can be read-only in some deployments; every other ``OSError``
    (permission on a single file, disk full, …) is a real problem and
    must re-raise unchanged.
    """
    return exc.errno == errno.EROFS or "read-only file system" in str(exc).lower()


def translate_readonly_fs(
    fn: Callable[..., Response],
) -> Callable[..., Response]:
    """Decorator for a write verb (``put``/``edit``/``delete``): turn a
    read-only-mount ``OSError`` into a typed :class:`Unsupported` instead of
    letting it surface as a raw ``[error:Internal] ... OSError``.

    Reads ``self.spec.kind`` for the response text, so it works unmodified
    on any :class:`~precis.protocol.Handler` subclass — the numeric-ref /
    plaintext-family kinds and ``Handler``-direct kinds like python alike.
    """

    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Response:
        try:
            return fn(self, *args, **kwargs)
        except OSError as exc:
            if not is_readonly_fs_error(exc):
                raise
            raise Unsupported(
                READONLY_FS_HINT,
                next=(
                    f"reads still work: get(kind='{self.spec.kind}', ...); "
                    "writes need the operator to mount the workspace rw"
                ),
            ) from exc

    return wrapper


__all__ = ["READONLY_FS_HINT", "is_readonly_fs_error", "translate_readonly_fs"]
