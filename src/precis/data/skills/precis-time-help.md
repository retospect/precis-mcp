---
id: precis-time-help
family: tools
title: precis — the time kind (clock, parse, windows, convert, format)
summary: a stateless, local, free clock — parse timestamps or resolve fixed fuzzy phrases and ISO date ranges to UTC windows; convert instants to any IANA zone with args={'to': ...}; render with args={'format': '<strftime>'}
answers:
  - what time is it right now, in UTC and locally?
  - how do I convert a timestamp to another time zone?
  - how do I get epoch seconds for a date, or a date for an epoch?
  - what date is "next monday" or "in 3 days"?
  - what UTC window does "about a month ago" or "last week" mean?
applies-to: get (kind='time')
kinds: [time]
status: active
tags: [workflow]
---

# precis-time-help — clock and timestamp arithmetic

`time` is **local, free, and stateless**. No args returns the current
instant; `q=` parses an instant or a supported window phrase. Point answers carry
three lines — UTC (ISO 8601, `Z`), the server's local time with its zone,
epoch seconds — so you never have to convert by hand.

```python
get(kind="time")
# utc:   2026-10-09T14:30:00Z (Friday)
# local: 2026-10-09T16:30:00+02:00 (Europe/Zurich)
# epoch: 1791556200
```

## What q= accepts

| shape | examples |
|-------|----------|
| ISO 8601 | `2026-10-09T14:30:00Z`, `2026-10-09 14:30`, `2026-10-09`, `2026-10-09T14:30:00+02:00` |
| RFC 2822 | `Fri, 09 Oct 2026 14:30:00 +0000` |
| epoch | `1791556200` (seconds), `1791556200123` (millis), `@1791556200.5` |
| common spellings | `9 Oct 2026 14:30`, `Oct 9, 2026`, `2026/10/09 2:30 pm`, `09.10.2026` |
| relative | `now`, `today`, `tomorrow`, `yesterday`, `in 3 hours`, `2 days ago`, `1 day 3 hours from now`, `2h30m ago`, `in 1 month` |
| weekday | `next monday`, `last friday`, `this wed`, `friday` (= next one) |

A parsed answer starts with a `parsed:` line showing the offset from
now (`in 3h`, `2d 4h ago`) so you can see which way it resolved.
`today` / `tomorrow` / weekdays resolve to **midnight**.

```python
get(kind="time", q="in 3 hours")
# parsed: 'in 3 hours' → in 3h
# utc:   2026-10-09T17:30:00Z (Friday)
# ...
get(kind="time", q="1791556200")  # epoch → date
```

Anything else refuses with the accepted shapes in the error and a
copy-pasteable `next=`. There is no free-form natural-language parser —
rewrite the phrase into one of the shapes above.

## UTC windows

These phrases resolve deterministically, with no model or free-form parser:

| q= | window | widened |
|----|--------|---------|
| `recently` | last 14 elapsed days, ending now | yes |
| `last week` | previous ISO week, Monday to Monday | no |
| `last month` | previous calendar month | no |
| `about a month ago`, `around a month ago` | 90 days ago to 30 days ago | yes |
| `a few months ago` | 180 days ago to 60 days ago | yes |
| `last year` | previous calendar year | no |
| `2026-10-01..2026-10-15` | October 1 midnight to October 15 midnight | no |

Bounds are **since-inclusive, until-exclusive**, rendered in UTC. Calendar
boundaries and range dates use `args={'from': ...}` when supplied, otherwise
UTC. Elapsed days are 24-hour periods measured from the UTC clock. Phrase
matching ignores case and surrounding whitespace. The reply echoes the
source and marks approximate phrases as `widened`.

```python
get(kind="time", q="about a month ago")
# At 2026-11-03T00:00:00Z:
# window: 2026-08-05T00:00:00Z … 2026-10-04T00:00:00Z (from 'about a month ago', widened)
# since='2026-08-05T00:00:00Z', until='2026-10-04T00:00:00Z' (until exclusive)
get(kind="time", q="2026-10-01..2026-10-15")
```

Existing point queries, including a bare date such as `2026-10-01`, keep
their point output. Unknown phrases and invalid or reversed ranges refuse
with accepted forms and a copy-paste `since=` /
`until=` example. Window queries accept `from`; `to` and `format` require
a point query and refuse on windows.

## Zones: naive input is UTC

A `q=` without an offset is read as **UTC**. To say otherwise, either
append the IANA zone to the text or pass `args={'from': ...}`:

```python
get(kind="time", q="2026-10-09 16:30 Europe/Zurich")  # → utc: 2026-10-09T14:30:00Z
get(kind="time", q="2026-10-09 14:30", args={"from": "America/New_York"})
```

## Convert and format

`args={'to': '<IANA zone>'}` adds a line in that zone; `args={'format':
'<strftime>'}` adds a formatted line — rendered in the `to` zone when
given, else UTC. Both combine with any point `q=`.

```python
get(kind="time", args={"to": "Asia/Tokyo"})
# ...
# Asia/Tokyo: 2026-10-09T23:30:00+09:00
get(kind="time", q="tomorrow", args={"to": "America/Los_Angeles", "format": "%a %d %b %H:%M %Z"})
# ...
# format: Fri 09 Oct 17:00 PDT
```

Zone names are IANA keys (`Europe/Zurich`, `America/New_York`,
`Asia/Tokyo`, `UTC`); a DST abbreviation like `PDT` or `CEST` is refused —
pick the city.

`time` vs `calc`: `calc` converts *durations* between units (`3 h to s`);
`time` places instants on the calendar and across zones.
