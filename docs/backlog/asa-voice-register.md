---
status: idea
title: asa voice — distinctive lexical register (avionics/Victorian/ja/cn/de) (gr51194)
---

# asa voice: distinctive lexical register (gr51194)

Consolidated from gripe 51194 (user-filed, tags: area:asa area:voice).
Taste-sensitive persona work — needs Reto's ear on the curation, not a
mechanical fix.

Current asa voice is terse but generic: the soul doc gestures at "a little
snarky" and lists bearing ideograms but gives no concrete vocabulary, so the
voice is consistent in rhythm but not texture.

Wanted: curate an asa-vocabulary skill or memory block (~30 terms with
usage notes) wired into the soul doc so sessions sample it when the
register fits — not every turn. Source registers from the gripe:

- **Avionics / mil-spec**: squawk, bingo (hitting a limit), bogey, vector,
  go/no-go, sortie, tally, fence check, gimbal lock, nominal, delta-V.
- **Victorian**: capital, rather, quite, bully, forthwith, I daresay,
  vexing, prodigious — compressed affect, dry understatement.
- **Japanese**: ma (間, negative space/pause), wabi, shibui, shoganai,
  ikigai, kaizen.
- **Chinese**: yuánfèn (緣分), chā bu duō (差不多, ironic "close enough"),
  mianzi, wúwéi (無為).
- **German**: Fingerspitzengefühl, Weltschmerz, Verschlimmbessern,
  Torschlusspanik, Drachenfutter.

Implementation seam: soul doc + a skill under `src/precis/data/skills/` or
an asa-side prompt block in `src/asa_bot/`.
