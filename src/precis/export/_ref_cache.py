"""Per-export memo over the store's repeated single-ref lookups.

An export is a read of one frozen draft, but the renderers resolve the same
cite slug at every ``[n]`` marker (``get_ref`` -> a ``cite_key`` identifier
query plus a ref fetch, then again for the doi/library link run, the SI
check and the reference list). On a 170-chunk draft that is thousands of
round trips; against the production database (~ms each) it was the ~30 s
the docx export took (gr493192).

:class:`RefLookupMemo` wraps the store for the lifetime of one export and
answers repeats of ``get_ref(kind=…, id=…)`` from memory. Everything else
is delegated untouched. Do not keep one across exports: it never
invalidates.
"""

from __future__ import annotations

from typing import Any


class RefLookupMemo:
    """Delegating store proxy that memoises ``get_ref`` (hits and misses)."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._refs: dict[tuple[Any, Any], Any] = {}
        self._idents: dict[Any, Any] = {}

    def get_ref(self, *args: Any, **kwargs: Any) -> Any:
        key = (args, tuple(sorted(kwargs.items())))
        try:
            return self._refs[key]
        except KeyError:
            pass
        except TypeError:  # unhashable argument — don't memoise
            return self._store.get_ref(*args, **kwargs)
        ref = self._store.get_ref(*args, **kwargs)
        self._refs[key] = ref
        return ref

    def identifiers_for_refs(self, ref_ids: Any) -> dict[Any, Any]:
        """Per-id memo over the batch identifier read: only ids not seen
        before reach the store; ids with no identifiers stay absent from
        the result, as in the store's own return."""
        ids = list(ref_ids)
        missing = [i for i in ids if i not in self._idents]
        if missing:
            got = self._store.identifiers_for_refs(missing)
            for i in missing:
                self._idents[i] = got.get(i)
        return {i: self._idents[i] for i in ids if self._idents[i] is not None}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)
