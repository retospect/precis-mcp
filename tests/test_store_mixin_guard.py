"""Guard against silent MRO shadowing across ``Store``'s domain mixins.

``store/store.py::Store`` composes ~21 mixins on convention alone —
each mixin is supposed to own a disjoint slice of the persistence
surface (see the module docstring in ``precis.store.store``). Nothing
in Python enforces that disjointness; a name collision between two
mixins resolves silently via MRO order, quietly shadowing one
implementation with another. This test walks Store's direct mixin
bases and asserts no method/attribute name is defined by more than
one of them, so a future copy-paste collision fails CI instead of
lurking until runtime.

The ``drafts`` domain (``DraftMixin``/``_AbbrevMixin``, now
:class:`~precis.store._draft_ops.DraftStore`) was carved out of this
mixin stack into a composed sub-store (``store.drafts`` — see
``docs/backlog/codereview-store-decomposition.md``) and is no longer
one of ``Store``'s direct bases, so it's out of scope for *this*
guard — same original guarantee (no silent MRO shadowing among
``Store``'s remaining mixins), just over a smaller (and shrinking, as
more domains are carved out this way) mixin set.

Pure introspection — no DB, no fixtures, import-only and fast.
"""

from __future__ import annotations

from collections import defaultdict

from precis.store.store import Store

# Names intentionally shared across mixins because they're a common
# constant/helper, not accidental duplication. If a new entry is ever
# needed here, explain why in a comment next to it. Cross-mixin
# forward-declaration stubs do NOT belong here — put them under
# ``if TYPE_CHECKING:`` in the declaring mixin instead (the ``add_tag``
# incident in ``_refs_ops.py``, repeated by ``CacheMixin`` until
# gripe 202377), so they never exist at runtime.
_ALLOWED_COLLISIONS: dict[str, set[str]] = {}


def _mixin_bases() -> tuple[type, ...]:
    """Store's direct bases, excluding ``object``."""
    return tuple(base for base in Store.__bases__ if base is not object)


def _own_member_names(cls: type) -> set[str]:
    """Names ``cls`` defines in its own ``__dict__`` (not inherited).

    Excludes dunders (``__init__``, ``__module__``, etc. — those are
    per-class bookkeeping, not part of the domain surface) but keeps
    both public and single-underscore-private names, since a private
    helper can shadow another mixin's private helper just as easily
    as a public method can.
    """
    names: set[str] = set()
    for name, value in vars(cls).items():
        if name.startswith("__") and name.endswith("__"):
            continue
        if not callable(value) and not isinstance(
            value, (property, staticmethod, classmethod)
        ):
            continue
        names.add(name)
    return names


def test_no_mixin_defines_a_name_owned_by_another_mixin() -> None:
    mixins = _mixin_bases()
    assert len(mixins) > 1, "expected Store to compose multiple mixins"

    owners: dict[str, list[str]] = defaultdict(list)
    for mixin in mixins:
        for name in _own_member_names(mixin):
            owners[name].append(mixin.__name__)

    collisions = {
        name: sorted(defining_mixins)
        for name, defining_mixins in owners.items()
        if len(defining_mixins) > 1
        and set(defining_mixins) != _ALLOWED_COLLISIONS.get(name, set())
    }

    assert not collisions, (
        "the following names are defined by more than one Store mixin, "
        "so MRO order silently decides which implementation wins:\n"
        + "\n".join(
            f"  {name!r}: {', '.join(defining_mixins)}"
            for name, defining_mixins in sorted(collisions.items())
        )
    )
