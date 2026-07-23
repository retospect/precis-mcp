---
id: precis-cron-help
title: precis — retired (folded into precis-recurring-help)
summary: kind='cron' is retired (ADR 0061, superseding ADR 0030) — scheduling + push delivery now live on level:recurring todos; see precis-recurring-help
applies-to: retired — no live surface
status: active
---

# precis-cron-help — retired

`kind='cron'` is **retired**. ADR 0061 (superseding ADR 0030) folded its two
jobs onto `level:recurring` todos:

* **Scheduling** (one-shot `when=`/`in_=`, or `recurring=`) — now
  `meta.schedule` on a `level:recurring` todo: `{'cron': '...'}` /
  `{'every': '...'}` for recurring, `{'at': '<iso>', 'catch_up': bool}` for
  one-shot.
* **Push delivery** (the synthetic-prompt-to-Discord mechanism) — now
  `meta.deliver = {'target': 'conv:discord/<g>/<c>/<t>'}` on the same
  recurring. A due tick fires the identical `pg_notify('precis.cron', ...)`
  wire payload asa_bot already listens for — no delivery-layer change.

See `precis-recurring-help` for the full surface (put shape, tick
mechanics, catch-up policy, delivery) and `precis-automations` for the
standing-automation pattern (podcast casts, news briefing). ADR 0061
(`docs/decisions/0061-fold-cron-into-recurring.md`) has the design
rationale for the fold.

## See also

```python
get(kind='skill', id='precis-recurring-help')   # the unified mechanism
get(kind='skill', id='precis-automations')      # find/edit standing automations
get(kind='skill', id='precis-message-help')     # proactive posts (verbatim, not a synthetic prompt)
```
