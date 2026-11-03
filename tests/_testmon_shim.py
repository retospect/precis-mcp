"""Let pytest-testmon fingerprint files that have no extension (gr558530).

testmon's ``SourceTree.get_file`` takes the extension as
``filename.rsplit(".", 1)[1]``, which raises IndexError for an extensionless
path (``scripts/*`` loaded in-process by a test). The crash surfaces in
``pytest_runtest_logreport`` as an INTERNALERROR, and ``scripts/ship
--impacted`` then reports "gate is RED" with no failing test.

:func:`install` wraps ``get_file`` so such a file gets ``ext=""``. testmon
fingerprints any non-``py`` extension as one whole-file block, so a change to
the script still re-selects the tests that ran it. A no-op when testmon is not
loaded (every lane but ``--impacted``/``--testmon``).
"""

from __future__ import annotations

import os
import sys
from typing import Any


def install() -> bool:
    """Patch testmon's ``SourceTree.get_file`` once; True if patched."""
    core = sys.modules.get("testmon.testmon_core")
    if core is None:
        return False
    tree_cls: Any = core.SourceTree
    if getattr(tree_cls.get_file, "_precis_ext_safe", False):
        return False

    def get_file(self: Any, filename: str) -> Any:
        if filename not in self.cache:
            code, fsha = core.get_source_sha(directory=self.rootdir, filename=filename)
            module = None
            if fsha:
                try:
                    module = core.Module(
                        source_code=code,
                        mtime=os.path.getmtime(os.path.join(self.rootdir, filename)),
                        ext=os.path.splitext(filename)[1].lstrip("."),
                        fs_fsha=fsha,
                        filename=filename,
                        rootdir=self.rootdir,
                    )
                except FileNotFoundError:
                    module = None
            self.cache[filename] = module
        return self.cache[filename]

    setattr(get_file, "_precis_ext_safe", True)
    tree_cls.get_file = get_file
    return True
