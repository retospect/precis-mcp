# Ingest ÖPG-CMD Graz 2026 Indico abstract book as precis ref

**what**: Ingest the ÖPG-CMD (Austrian Physical Society, Condensed Matter Division) Graz 2026 conference Indico abstract book as a `cfp` or similar reference so conference speakers and their abstracts become searchable and linkable by Reto's sessions.

**why**: The Graz conference notes thread (td437627) uses the abstract book to cross-reference speaker names and session abstracts. Currently the book is external; ingesting it into precis makes session-query-to-speaker resolution possible.

**owner anchor**: `src/precis/ingest/` · `src/precis/handlers/cfp.py` (if using cfp kind) or similar handler · ÖPG-CMD Graz 2026 Indico URL

**test**: n/a — requires verifying that speakers and abstract snippets from the book resolve via search and link back to the conference entry.
