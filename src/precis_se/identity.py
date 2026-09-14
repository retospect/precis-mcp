"""Addressing a block: by ``uid`` always, by label when it is unambiguous.

The other half of the identity cutover (:mod:`precis_se.persist`,
migration ``0009_se_block_uid.sql``, docs/backlog/design-state-core.md
item 2). Storage now keys every cross-reference by uid; this is what lets
an *agent* speak the same identity — ``args={'name': '#41'}`` addresses
block uid 41 whatever it is called today.

**Token forms.** An ``int`` is a uid. A string is a uid when it is
``'#41'`` or ``'uid:41'`` — ``'#'`` is already reserved out of block names
(:func:`precis.blocktree.types.parse_template_ref` splits on it), so
neither form can collide with a label. Anything else is a **label**, and a
label that happens to be all digits is still a label first: a block
actually called ``'12'`` resolves to itself, and only a label that matches
nothing falls through to being read as a uid. Nothing silently hijacks a
name.

**Ambiguity.** A label is unique within a live design today (the
``se_blocks_ref_name_key`` index stands — item 2 keeps label uniqueness
for now), so :class:`AmbiguousLabel` cannot fire from a stored tree. It is
built and tested anyway because it is the path that makes relaxing that
index a *migration* rather than a redesign, and because a tree assembled
in memory (a branch merge, a paste) can already hold two blocks under one
label before anything writes. The error is structured — it carries the
label and every matching uid — so the caller can retry with one of them
rather than guess.
"""

from __future__ import annotations

from typing import Any

from precis.blocktree.types import UID_PREFIX, OpError

#: The ``'#41'`` / ``'uid:41'`` prefixes that force a token to be read as a
#: uid rather than a label. The second is the one block names are reserved
#: against at mint time (:func:`precis.blocktree.ops._reject_reserved_name`
#: — ``'#'`` was already reserved by the template syntax), so the
#: reservation and the parser read the same constant.
_UID_PREFIXES = ("#", UID_PREFIX)


class AmbiguousLabel(OpError):
    """Two or more live blocks answer to one label.

    Carries the machine-readable parts (``label``, ``uids``) beside the
    message so a caller can offer the choice instead of re-parsing prose.
    An :class:`~precis.blocktree.types.OpError` (itself a ``ValueError``)
    so it needs no translation when it fires from inside an op: the ops
    layer's own error type reaches the handler with the uid list intact.
    """

    def __init__(self, label: str, uids: list[int]) -> None:
        self.label = label
        self.uids = sorted(uids)
        listed = ", ".join(f"#{u}" for u in self.uids)
        super().__init__(
            f"block label {label!r} is ambiguous — {len(self.uids)} blocks "
            f"answer to it ({listed}); address it by uid instead, e.g. "
            f"'#{self.uids[0]}'"
        )


def parse_uid(token: Any) -> int | None:
    """``token`` read as a uid, or ``None`` when it is not one of the uid
    forms. Pure syntax — it never looks at a tree, so an unknown uid parses
    fine and fails to *resolve* later, which is the honest split."""
    if isinstance(token, bool):  # bool is an int; a flag is not a uid
        return None
    if isinstance(token, int):
        return token
    if not isinstance(token, str):
        return None
    raw = token.strip()
    for prefix in _UID_PREFIXES:
        if raw.startswith(prefix):
            body = raw[len(prefix) :].strip()
            return int(body) if body.isdigit() else None
    return None


def blocks_by_label(tree: Any, label: str) -> list[Any]:
    """Every block in ``tree`` answering to ``label``.

    Scans the blocks' own ``name`` rather than taking the dict key, so two
    blocks carrying one label are *found* rather than silently collapsed
    into whichever one the mapping happens to hold — which is what makes
    :class:`AmbiguousLabel` reachable before the DB index relaxes. A
    design is tens of blocks; the O(n) is not worth an index that every op
    would have to keep true. The dict-key lookup is kept as the fallback
    for a tree whose keys and names have diverged the other way.
    """
    matches = [node for node in tree.blocks.values() if node.name == label]
    if matches:
        return matches
    node = tree.blocks.get(label)
    return [node] if node is not None else []


def block_by_uid(tree: Any, uid: int) -> Any | None:
    """The block carrying ``uid``, or ``None``. Linear over the tree: a
    design is tens of blocks, and an index would be one more thing to keep
    true across every op that adds one."""
    for node in tree.blocks.values():
        if node.uid is not None and int(node.uid) == uid:
            return node
    return None


def resolve_block(tree: Any, token: Any) -> Any | None:
    """``token`` → its block, or ``None`` when nothing answers to it.

    Raises :class:`AmbiguousLabel` when the token is a label two blocks
    share — the one case that is neither a hit nor a miss. Callers keep
    their own "no such block" message (``_block_not_found`` lists the
    design's labels), so the miss is a plain ``None`` here.
    """
    uid = parse_uid(token)
    if uid is not None:
        return block_by_uid(tree, uid)
    label = str(token).strip()
    matches = blocks_by_label(tree, label)
    if len(matches) > 1:
        raise AmbiguousLabel(label, [int(n.uid) for n in matches if n.uid is not None])
    if matches:
        return matches[0]
    # A label that matches nothing, read as a bare uid: '41' addresses uid
    # 41 only once it is certain no block is CALLED '41'.
    if label.isdigit():
        return block_by_uid(tree, int(label))
    return None
