"""precis-chem plugin migrations.

Discovered via the ``precis.migrations`` entry-point group (namespace
``precis_chem``). The core migration runner (:class:`precis.store.Migrator`)
resolves this package to its directory and applies every ``*.sql`` here
whose ``(plugin, version)`` isn't already in the ``_migrations`` ledger —
after the built-in ``precis`` source, so core schema (incl. the
``kinds`` / ``relations`` reference tables + the 0023 plugin column) is
in place first.
"""
