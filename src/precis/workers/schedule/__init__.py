"""Slice 4 schedule worker package.

Carries:

* :mod:`.parse` — minimal cron parser + ``every:`` shorthand
  translator. Single point of truth for write-time validation
  (called from ``handlers/_todo_guards``) and tick expansion (called
  from :mod:`.worker`).
* :mod:`.seed` — idempotent Watches umbrella seeder used by both the
  worker's first-run path and the handler's recurring-root default.
* :mod:`.worker` — the per-pass spawner driven by ``precis worker
  --only schedule``.

The CLI wires ``run_schedule_pass`` into the default rotation
alongside :mod:`precis.workers.auto_check`; the two passes share the
same ref-level pattern (no chunk-level claim-row, just SQL +
short-lived transactions).
"""

from precis.workers.schedule.parse import (
    Schedule,
    parse_schedule,
    ticks_since,
    validate_schedule,
)
from precis.workers.schedule.seed import (
    WATCHES_BUILTIN,
    WATCHES_TITLE,
    ensure_watches_root,
)
from precis.workers.schedule.worker import run_schedule_pass

__all__ = [
    "WATCHES_BUILTIN",
    "WATCHES_TITLE",
    "Schedule",
    "ensure_watches_root",
    "parse_schedule",
    "run_schedule_pass",
    "ticks_since",
    "validate_schedule",
]
