# `meta` JSON-path filter convention

19+ call sites filter on `meta->…` with no GIN/expression index — safe
only because always paired with indexed `kind`. Decide: index the hot
paths, or write the "pre-filter by an indexed column" rule into the
store docstring so the next kind doesn't violate it unknowingly. Needs
a small design decision, then mechanical.
