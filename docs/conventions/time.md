# Convention — one zone, and it is UTC

**Every timestamp this system generates is UTC, and every absolute timestamp a
human reads says so.** No exceptions, no per-host zones, no "it's UTC on the
fleet so it's fine."

The fleet already enforces its half: `deploy/playbooks/00a-timezone.yml` pins
each node's system zone, deploy-rendered units carry `TZ=UTC`,
`utils/utc_logging.py::force_utc_timestamps` pins `logging.Formatter` to
`time.gmtime`, and every timestamp column in `src/precis/migrations/` is
`timestamptz`. What that does *not* cover is a developer's laptop, which is on
whatever zone its owner lives in — and the artefacts a laptop writes outlive the
session that wrote them.

That is not hypothetical. `.deploy-logs/` filenames were stamped with a bare
`date +%Y%m%d-%H%M%S`, so they rendered in the laptop's local zone and carried
no marker saying which. Reading one as UTC put a host's clock 45 minutes out
and sent a session chasing a clock-skew bug that did not exist, until `date -u`
disproved it. An unlabelled local timestamp is worse than no timestamp: it
looks authoritative and is wrong by an amount nobody can see.

## The three rules

**1. Generate in UTC.**

```sh
date -u +%Y%m%d-%H%M%S       # yes
date +%Y%m%d-%H%M%S          # no — local zone, silently
date +%s                     # fine — epoch seconds carry no zone at all
```

```python
datetime.now(UTC)            # yes
datetime.now(UTC).date()     # yes — the UTC-correct `date.today()`
date.today()                 # no
datetime.now()               # no — naive
datetime.utcnow()            # no — naive, and deprecated since 3.12
```

This applies to *parsing* too. `date -d "2026-09-18"` and `date -j -f` resolve a
bare date in the local zone, so reading a UTC-written stamp without `-u` is off
by the offset: `epoch_of()` in `scripts/nightly`, `scripts/memory-lint` and the
`*-review` scripts carries `-u` on both branches for that reason.

**2. Coerce at the boundary.** Anything arriving as a string — a stored ISO
stamp, an API header — may be naive or may carry an offset. Run it through
`precis_web/timefmt.py::_as_datetime`, which coerces naive to UTC and converts
aware values into it. Doing arithmetic against `datetime.now(UTC)` without that
step raises `TypeError` on naive input; formatting without it prints the wrong
hour under a hardcoded `UTC` label.

**3. Label what humans read.** An absolute time rendered to a page, a log line,
or an agent's output ends in `Z` or ` UTC`. `precis_web/timefmt.py::abs_ts` is
the renderer — use it rather than a bare `.strftime`, which inherits the process
zone and says nothing about which one it got. Relative times ("3h ago") need no
label.

## Enforcement

`tests/test_utc_time_convention.py` scans `src`, `tests`, `scripts`, `docker`
and `deploy` for shell `date` calls missing `-u` (epoch excepted) and
AST-walks Python for `date.today()`, bare `datetime.now()` and
`datetime.utcnow()`. A new local-time stamp reddens the ship.

`tests/test_utc_logging.py` and `tests/precis_web/test_timefmt.py` cover the
rendering half.
