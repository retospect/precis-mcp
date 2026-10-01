---
status: draft
title: Per-user library discovery link for citations and export
pillar: personal
---

# Per-user library discovery link for citations and export

Draft export jobs (LaTeX/docx) emit library hyperlinks next to every
citation. Web paper/finding pages also surface library links. Both currently
resolve to a single institution's discovery service, configured at
install-time via `PRECIS_LIBRARY_SEARCH_URL` / `PRECIS_LIBRARY_LABEL` /
`PRECIS_LIBKEY_LIBRARY_ID` in `PrecisConfig` (defaults: University of
Limerick Primo). A shared install serves users from different institutions;
the link should follow the reader, not the server.

## Motivation / why

In a multi-user instance, one person may have library access via their home
institution while a colleague does not. A standard journal link resolver or
library discovery service varies by the user's institutional credentials and
network. The current install-wide default cannot adapt per viewer.

## In scope

1. **Per-user library preference record**: server-side storage (label, search
   URL template with `{query}`, optional LibKey library ID), keyed by web
   auth identity.
2. **Web paper/finding pages**: read the authenticated viewer's library
   preference; fall back to install default if unset.
3. **Draft export jobs**: carry the requesting user's identity (or explicit
   `library=` override in job payload) so the exporter renders that user's
   library link.
4. **User settings**: a web settings pane and/or CLI verb to get/set library
   preference.
5. **Fallback chain**: unset user preference → install default.

## Explicitly NOT in scope

- Changing what the DOI link does or how it resolves.
- Per-draft overrides at the document level.
- MCP-agent-triggered exports (no web auth identity); these fall back to
  install default.

## Acceptance criteria

- Two authenticated web users with different library preferences see
  different library hyperlinks on the same paper page.
- An export job run for each user renders their own library link.
- An unauthenticated or unset-preference export falls back to install
  default.
- Tests cover the preference-lookup and fallback chain.
- Web auth binds library preference to identity and persists across sessions.

## Target + blast radius

`src/precis/utils/paper_links.py` · `src/precis_web/paper_links.py` ·
`src/precis/export/latex.py::_cite_link_group` ·
`src/precis/export/docx.py::_cite_link_group` · `src/precis/config.py`
(library_* fields) · `src/precis_web/config.py` (auth integration) · web
user settings route · export job schema (identity/library override field) ·
worker/exporter integration to read user context.

## Open questions / decisions log

None at spec time.
