"""Stand-in for ``scripts/prod-precis`` behind ``PRECIS_LAND_NOTIFY_CLI`` and
``PRECIS_GRIPE_NOTE_CLI``.

``scripts/lib/land_notify.py`` and ``scripts/gripe-note`` write to the
production graph (through ``scripts/prod-precis``) whenever a prod DSN file
exists under the home directory. The scratch world's home has none, and these
two variables name this stub as well, a second fence: nothing is posted
anywhere, every call is appended to ``<world>/logs/notify-calls.jsonl`` and
exits 0, so the notify path of a land is exercised and can be asserted on.
Standard library only.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    log = Path(argv[0])
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "argv": argv[1:]}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
