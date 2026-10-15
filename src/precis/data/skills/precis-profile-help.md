---
id: precis-profile-help
family: files
title: precis — the profile kind (owner-scoped branding profiles)
summary: kind='profile' stores a per-user poster/deck branding profile — palette (role→hex), font, logo strip (figure ids), sponsor line — validated by the poster exporter's rules; export picks meta.workspace.poster.profile, else the draft owner's default
answers:
  - how do I store my poster/branding palette and logo strip?
  - how does a poster draft pick which branding profile to export under?
  - what are the palette roles and what makes a profile invalid?
applies-to: put/edit/get/delete (kind='profile'); doc_type='poster' drafts
tags: workflow, verbs
kinds: profile, draft, figure
status: active
---

# precis-profile-help — the `profile` kind

A **profile** is an owner-scoped branding preset for `doc_type='poster'`
drafts (a role→hex palette, one allowed font, an ordered logo strip of
`kind='figure'` refs, a sponsor line). It is *data*, not a template —
chrome and typography stay in the generic `poster.tex`; a profile only
swaps hues, the sans family and the strip.

Fields (Reto ruling 2026-10-15): `name`, `palette` (role→`#RRGGBB`),
`fonts` (`{'body': 'lmodern'|'helvet'}`), `logos` (figure ids),
`sponsor_line`, `owner` (a `web_users` login), `default` (the owner's
fallback profile for exports that name none).

## Verbs

```python
put(kind='profile', name='ul-bernal', owner='<login>',
    palette={'accent': '#005335', 'heading': '#005335', 'band': '#003726',
             'bodytint': '#EFF2EF', 'citation': '#373A36',
             'measured': '#005335', 'computed': '#007DBA',
             'bandtext': '#FFFFFF', 'bodytext': '#000000'},
    fonts={'body': 'helvet'},
    logos=[{'figure': 'ul-logo', 'alt': 'University of Limerick'},
           {'figure': 'erc-logo', 'group': 'sponsor', 'alt': 'ERC'}],
    sponsor_line='Funded by …', default=True)
get(kind='profile')                    # your profiles (all, for agents)
get(kind='profile', id='ul-bernal')    # by id, pf<N> handle, or name
edit(kind='profile', id=pf7, palette={'accent': '#003726'})
delete(kind='profile', id=pf7)
```

`edit` merges `palette`/`fonts` per key and replaces the other fields.

**ACL — owner-only.** Profiles are private to their `owner` login: a
call carrying another user's identity can't read or write them. Calls
with no user identity (fleet `agent`, CLI) pass — the guard separates
users, not the deployment's own agents.

## Validation

`put`/`edit` run the poster validators and return their codes:
`poster_role_missing` / `poster_role_unknown` / `poster_role_invalid`
(the palette must fill exactly the nine declared roles), `poster_font_unknown`,
`poster_contrast_insufficient` (WCAG 4.5:1 floor on `bandtext`↔`band`/
`heading` and `bodytext`↔`bodytint`), `poster_logo_missing`,
`poster_logo_resolution_low` (300 ppi at rendered size),
`poster_logo_too_large` / `poster_logo_pixels_exceeded` /
`poster_logo_multipage` / `poster_logo_count`, and
`poster_profile_too_large` (64 KiB JSON cap).

Roles: `accent` `heading` `band` `bodytint` `citation` `measured`
`computed` `bandtext` `bodytext` — the `\definecolor` names in the
generic `poster.tex`.

## Resolution at export

A `doc_type='poster'` export resolves its branding in order:

1. `meta.workspace.poster` — set `profile` to a profile id/`pf<N>`/name
   to pin one, with any field keys layered on top as inline overrides;
2. the draft owner's `default` profile (`refs.owner_login` — the account
   that created the draft, not the first byline author);
3. the neutral product palette.

A `profile` selector that resolves to nothing fails the export —
fix the pointer rather than shipping wrong branding.
