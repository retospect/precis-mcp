---
name: clock-basics
description: >
  Tell the time, today's date, and how long until / since any date.  No
  network, no extras — pure stdlib (`datetime` / `zoneinfo`).  Use any
  time the agent needs to ground itself in absolute time, compute a
  duration, or convert between IANA timezones.  Free, deterministic.
user-invocable: true
argument-hint: [<query>]
allowed-tools: [get]
applies-to: [clock]
kind-onboarding: clock
tags: [time, date, timezone, duration]
---

## When to use

- The agent needs to know what time / date it is *now* — LLMs cannot.
- Compute a duration (`how long until 1 Jan 2027`).
- Convert between timezones (`now in Tokyo`).
- Resolve a named milestone (`christmas`, `eoq`, `next-friday`).

`clock:` is read-only.  Anything that mutates state (`note`, `replace`,
…) returns `mode_unsupported`.  For relative time *inside* an existing
ref, write to that ref directly.

## Common shapes

```
get(id='clock:')                      → rich default (UTC + local + ISO)
get(id='clock:utc')                   → ISO 8601 UTC
get(id='clock:Europe/Dublin')         → any IANA tz
get(id='clock:unix')                  → epoch seconds
get(id='clock:date')                  → today, UTC
get(id='clock:date/America/New_York') → today, in tz
get(id='clock:?format=%Y-%m-%d')      → custom strftime
```

## Durations

```
get(id='clock:until/2027-01-01')                      → days until
get(id='clock:until/2026-12-25T18:00')                → to a datetime
get(id='clock:since/2025-01-01')                      → elapsed
get(id='clock:between/2026-04-01/2026-12-31')         → span
get(id='clock:until/eoq')                             → end of quarter
get(id='clock:until/new-year')                        → to next 1 Jan
get(id='clock:until/christmas')                       → 25 Dec
get(id='clock:until/next-friday')                     → soonest Fri
```

## Date format rules

- ISO 8601 only.  `YYYY-MM-DD` for dates, `YYYY-MM-DDTHH:MM` for datetimes.
- Ambiguous formats (`01/02/2027` could be 1 Feb or 2 Jan) are refused
  with both interpretations shown.
- Two-digit years are refused — write the full four digits.

## Named shorthands

`new-year`, `christmas`, `easter-YYYY`, `eoy`, `eoq`, `eom`, `eow`,
`tomorrow`, `yesterday`, `next-monday` … `next-sunday`.

## See also

- `get(id='clock:/zones')` — common IANA timezone reference.
- `get(id='clock:/help')` — same content as this skill, inline.
