"""Plugin migration root for the `estimate` kind.

precis-mcp's migrator resolves the ``precis.migrations`` entry point
(``precis_estimate = "precis_estimate.migrations"``) to *this package's
directory* and applies the ``*.sql`` files in it under the plugin namespace
``precis_estimate`` (ADR 0005: forward-only, idempotent) — mirrors
``precis_pathway.migrations`` / ``precis_chem.migrations`` /
``precis_bio.migrations``.
"""
