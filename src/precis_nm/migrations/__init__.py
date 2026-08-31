"""precis-nm plugin migrations.

Discovered via the ``precis.migrations`` entry-point group (namespace
``precis_nm``). The core migration runner (:class:`precis.store.Migrator`)
resolves this package to its directory and applies every ``*.sql`` here
whose ``(plugin, version)`` isn't already in the ``_migrations`` ledger —
after the built-in ``precis`` source, so core schema (incl. the ``kinds``
reference table + the ``refs`` table) is in place first.
"""
