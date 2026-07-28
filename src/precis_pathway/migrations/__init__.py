"""Plugin migration root for the autocatpath `pathway` kind.

precis-mcp's migrator resolves the ``precis.migrations`` entry point
(``autocatpath = "precis_pathway.migrations"``) to *this package's directory*
and applies the ``*.sql`` files in it under the plugin namespace ``autocatpath``
(ADR 0005: forward-only, idempotent). The entry-point key is the namespace —
kept as ``autocatpath`` (not ``precis_pathway``) so it matches the namespace
already recorded in prod's ``_migrations`` ledger from when the glue lived in
the external ``autocatpath`` package: this migration re-checks as a no-op
there, rather than re-applying under a new namespace.
"""
