"""Autocatpath engine-version token — the re-key lever for engine-keyed caches.

Neutral home (``precis.structure`` is inside the ``precis-geom`` wheel's
import allow-list) for the pin-derived token that ``precis.quest.compute``
folds into its idem keys and ``precis.structure.neb`` folds into the NEB
cache address. Moved out of ``precis.quest.compute`` so the wheel-side
``structure/neb.py`` need not import the quest package; compute re-imports
every name here, so existing ``compute._autocatpath_*`` references keep
resolving to the same objects (including ``cache_clear`` on the pin lookup).

See ``precis.quest.compute``'s notes on why the token tracks the dist pin,
and the ``PRECIS_AUTOCATPATH_VERSION`` ops escape hatch.
"""

from __future__ import annotations

import functools
import importlib.metadata
import os
import re

#: Engine-version token folded into the autocatpath idem key so deploying a new
#: autocatpath build auto-invalidates stale completed jobs. Without it, a
#: re-dispatch of the same (config, slab) dedupes onto the old completed job and
#: never exercises new engine code — the qu164903 "empty frontier" trap: all 21
#: candidates were pinned on autocatpath 0.1.1's desorption false-positives (102
#: phantom "detached" warnings → barrier_trusted=false) and never re-scored on
#: 0.4.0, which relaxes the same geometries cleanly (0 detached, trusted).
#: The token is DERIVED from the ``autocatpath`` floor pin in precis's own
#: dist metadata (:func:`_autocatpath_pinned_version`), because every engine
#: adoption already bumps that pin in the same commit — one lever, no
#: separate hand-bump to forget (the 0.4.0/0.6.0/0.7.0 adoptions each had to
#: remember this constant; see the shipped
#: autocatpath-060-selectivity-objectives proposal, git history). The env
#: var stays
#: as an ops escape hatch (force a re-key without a ship); the constant is a
#: last-resort fallback for a venv with no precis dist metadata. Remaining
#: caveat (unchanged from before): the token tracks the *pin*, not what
#: spark's GPU venv actually runs — an engine release adopted without a pin
#: bump still dedup-pins stale jobs, so keep bumping the pin with every
#: adoption.
_AUTOCATPATH_VERSION_ENV = "PRECIS_AUTOCATPATH_VERSION"
#: Kept in sync with the pyproject ``autocatpath`` pin floor so the
#: metadata-less fallback re-keys with the primary derivation; the pin bump is
#: the real re-key lever (:func:`_autocatpath_pinned_version` wins whenever
#: dist metadata exists, which it always does on a dispatch host).
_AUTOCATPATH_CACHE_EPOCH = "0.18.0"

#: Matches the ``autocatpath`` requirement (any extras) in dist metadata,
#: e.g. ``autocatpath>=0.7; extra == "catalyst"`` / ``autocatpath[mace]>=0.7``.
_AUTOCATPATH_REQ_RE = re.compile(r"^autocatpath\s*(?:\[[^]]*\])?\s*[<>=!~(]")


@functools.cache
def _autocatpath_pinned_version() -> str | None:
    """Floor of the ``autocatpath`` pin in the installed ``precis-mcp`` dist
    metadata, normalized to three components (``0.7`` → ``0.7.0`` — keeps the
    derived token byte-identical to the hand-bumped epochs it replaces, so
    adopting this derivation re-keys nothing). Extras-marked requirements are
    listed in metadata regardless of which extras a venv installed, so this
    works on dispatch hosts that never install the engine itself (compute
    keeps its no-``autocatpath``-import rule). ``None`` when precis isn't
    installed as a dist or carries no ``>=`` pin.

    Cached for the process lifetime — dist metadata cannot change under a
    running process; a test that fakes metadata calls ``cache_clear()``
    (the fixture in ``TestAutocatpathContentKey`` does). Resolution order
    lives in :func:`_autocatpath_engine_token`: the
    ``PRECIS_AUTOCATPATH_VERSION`` env override is re-read on EVERY call
    there, deliberately outside this cache — ``monkeypatch.setenv`` is the
    sanctioned test seam and needs no ``cache_clear``."""
    try:
        reqs = importlib.metadata.requires("precis-mcp") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    floors: list[str] = []
    for req in reqs:
        if not _AUTOCATPATH_REQ_RE.match(req):
            continue
        m = re.search(r">=\s*([0-9][0-9A-Za-z.]*)", req.split(";", 1)[0])
        if not m:
            continue
        parts = m.group(1).split(".")
        while len(parts) < 3:
            parts.append("0")
        floors.append(".".join(parts))
    if not floors:
        return None

    # The pin appears once per extra (catalyst, catalyst-gpu); if they ever
    # diverge, the HIGHEST floor is the one that must re-key (under-keying is
    # the dedup-pin trap this whole derivation exists to close).
    def _floor_key(v: str) -> tuple[int, ...]:
        out = []
        for p in v.split("."):
            d = re.match(r"\d+", p)
            out.append(int(d.group()) if d else 0)
        return tuple(out)

    return max(floors, key=_floor_key)


def _autocatpath_engine_token() -> str:
    """Engine-version component of the idem key: ``PRECIS_AUTOCATPATH_VERSION``
    env override if set, else the pin-derived version, else the code-constant
    fallback. A change in this value re-keys every (config, slab) pair, so a
    new engine build re-evaluates candidates instead of reusing stale results.

    Resolution is deliberately non-uniform: the env read happens on every
    call (ops escape hatch and the ``monkeypatch.setenv`` test seam), while
    the dist-pin lookup is ``functools.cache``d for the process lifetime —
    see :func:`_autocatpath_pinned_version`."""
    return (
        os.environ.get(_AUTOCATPATH_VERSION_ENV)
        or _autocatpath_pinned_version()
        or _AUTOCATPATH_CACHE_EPOCH
    )
