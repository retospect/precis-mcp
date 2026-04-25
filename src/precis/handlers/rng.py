"""RngHandler — random number generator.

Stateless, read-only, free.  Pure stdlib (``random`` / ``secrets`` /
``uuid``); no external deps.

Why this is a separate kind, not bolted onto ``calc:``: ``calc:`` is
intentionally deterministic and AST-sandboxed.  Adding randomness
would muddy both its testability story and its security story.
``rng:`` is its own one-tool kind.

Why this is separate from ``random:``: ``rng:`` is a stdlib math
primitive; ``random:`` is a vector-database content sampler.  They
share the word but nothing else — different cost, availability,
seed semantics.

Primary currency is **integers and ranges**.  Float is opt-in.  Default
call returns a coin flip (int 0 or 1) — the highest-value terse call
because agents reach for RNG to make a single yes/no / branch decision
far more often than they need a float.

Surface (path is the entire opaque path after ``rng:``):

| URI                          | Returns                            |
|------------------------------|------------------------------------|
| ``rng:``                     | int [0, 1] — coin flip             |
| ``rng:100``                  | int [0, 100] inclusive             |
| ``rng:1..6``                 | int [1, 6] inclusive               |
| ``rng:1..6x4``               | list of 4 ints in range            |
| ``rng:float``                | float [0.0, 1.0)                   |
| ``rng:float/0..1``           | float in range, [lo, hi)           |
| ``rng:3d6``                  | dice — 3 six-sided + sum           |
| ``rng:choice/red,green,blue``| uniform pick                       |
| ``rng:shuffle/a,b,c,d``      | list in random order               |
| ``rng:uuid``                 | UUID4                              |
| ``rng:bytes/16``             | 16 random bytes, hex-encoded       |
| ``rng:?seed=42/3d6``         | seeded — same call → same result   |
| ``rng:/help``                | onboarding skill inline            |

Inclusivity convention:

- Integer ranges are inclusive on both ends (``1..6`` returns 1..6 — the
  dice / "pick a number between 1 and 6" intuition).
- Float ranges are ``[lo, hi)`` (matches ``random.uniform`` /
  ``np.random.rand`` so results feed cleanly into downstream math).

Crypto safety: ``rng:bytes/<n>`` and ``rng:uuid`` always use
``secrets`` / ``uuid4`` regardless of ``?seed=`` — seeding crypto is
a footgun.  A warning is emitted when ``?seed=`` is combined with
those modes.
"""

from __future__ import annotations

import logging
import random as _random
import re
import secrets
import uuid
from typing import ClassVar

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_DICE_RE = re.compile(r"^(\d+)d(\d+)$")
_INT_RANGE_RE = re.compile(r"^(-?\d+)\.\.(-?\d+)(?:x(\d+))?$")
_FLOAT_RANGE_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)(?:x(\d+))?$"
)
_INT_SINGLE_RE = re.compile(r"^(-?\d+)$")

_MAX_LIST_LEN = 1000  # cap on multi-sample / shuffle output
_MAX_BYTES = 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_query(s: str) -> tuple[str, dict[str, str]]:
    """Split ``foo/bar?a=1&b=2`` into ``("foo/bar", {"a": "1", "b": "2"})``.

    Two URI shapes are accepted because both feel natural to type:

    1. **Trailing query**: ``rng:3d6?seed=42`` — body before, params after.
       This is the conventional URL form.
    2. **Leading query**: ``rng:?seed=42/3d6`` — params at the top, body
       after the last param.  When ``?`` appears first the body is
       everything after the LAST ``/`` in the final param value, and
       that param value is itself shortened to whatever was before
       the ``/``.

    Form (2) lets agents prefix every call with a seed without having
    to also know where to stick the path; form (1) is what most code
    written by hand would prefer.  Both round-trip cleanly through
    this function.
    """
    if "?" not in s:
        return s, {}
    head, qs = s.split("?", 1)
    params: dict[str, str] = {}
    pairs = qs.split("&")
    for kv in pairs:
        if not kv:
            continue
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v
        else:
            params[kv] = ""
    # Form (2): the URI started with ``?``, so ``head`` is empty and the
    # body is hidden in the final value.  Peel off any trailing ``/path``
    # from the final-key value and surface it as the body.
    if not head and pairs:
        last_key = pairs[-1].split("=", 1)[0] if "=" in pairs[-1] else pairs[-1]
        last_val = params.get(last_key, "")
        if "/" in last_val:
            shortened, body_tail = last_val.split("/", 1)
            params[last_key] = shortened
            head = body_tail
    return head, params


def _parse_seed(params: dict[str, str]) -> int | None:
    """Pull ``?seed=<int>`` out of params; return None when unset."""
    if "seed" not in params:
        return None
    raw = params["seed"]
    try:
        return int(raw)
    except ValueError as exc:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"?seed= must be an integer; got {raw!r}",
        ) from exc


def _make_rng(seed: int | None) -> _random.Random:
    """Build an isolated Random instance.  Seed=None → OS randomness."""
    return _random.Random(seed) if seed is not None else _random.Random()


def _seeded_label(seed: int | None) -> str:
    return str(seed) if seed is not None else "os"


def _clamp_count(n: int, *, kind: str) -> int:
    """Clamp a count parameter to the safety cap with a clean error."""
    if n < 1:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"{kind} count must be ≥ 1; got {n}",
        )
    if n > _MAX_LIST_LEN:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"{kind} count {n} exceeds cap {_MAX_LIST_LEN}",
        )
    return n


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------


def _footer(seed: int | None) -> str:
    return f"\n\n---\n_Generated locally by precis.rng · seed={_seeded_label(seed)}_"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class RngHandler(Handler):
    """Handler for the ``rng:`` scheme — random number generator.

    Agent usage::

        get(id='rng:')                    — coin flip 0 or 1
        get(id='rng:1..6')                — pick a number 1-6
        get(id='rng:3d6')                 — three six-sided dice
        get(id='rng:choice/red,green')    — uniform pick
        get(id='rng:shuffle/a,b,c,d')     — random order
        get(id='rng:uuid')                — UUID4
        get(id='rng:?seed=42/3d6')        — seeded
    """

    scheme = "rng"
    writable = False
    views: ClassVar[set[str]] = {"help"}
    onboarding_skill = "rng-basics"

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        raw = (path or "").strip()
        body, params = _split_query(raw)
        seed = _parse_seed(params)
        return self._dispatch(body, params, seed)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self, body: str, params: dict[str, str], seed: int | None
    ) -> str:
        # Special views (also accept bare-leading-slash forms)
        if body in {"/help", "help"}:
            return self._help()

        # Empty body → coin flip default
        if body in {"", "/"}:
            return self._coin_flip(seed)

        rng = _make_rng(seed)

        # uuid (CSPRNG always)
        if body == "uuid":
            if seed is not None:
                log.warning("rng:uuid ignores ?seed= — uuid4 is CSPRNG-backed")
            return f"{uuid.uuid4()}{_footer(None)}"

        # bytes/<n> (CSPRNG always)
        if body.startswith("bytes/"):
            return self._bytes(body[len("bytes/"):], seed)

        # choice/<comma list>
        if body.startswith("choice/"):
            return self._choice(body[len("choice/"):], rng, seed)

        # shuffle/<comma list>
        if body.startswith("shuffle/"):
            return self._shuffle(body[len("shuffle/"):], rng, seed)

        # float / float/<lo..hi>
        if body == "float":
            return f"{rng.random():.17f}{_footer(seed)}"
        if body.startswith("float/"):
            return self._float_range(body[len("float/"):], rng, seed)

        # NdM dice notation
        m = _DICE_RE.match(body)
        if m:
            return self._dice(int(m.group(1)), int(m.group(2)), rng, seed)

        # a..b (with optional xN)
        m = _INT_RANGE_RE.match(body)
        if m:
            return self._int_range(
                int(m.group(1)), int(m.group(2)),
                int(m.group(3)) if m.group(3) else 1,
                rng, seed,
            )

        # Single integer N → [0, N]
        m = _INT_SINGLE_RE.match(body)
        if m:
            n = int(m.group(1))
            if n < 0:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause=f"rng:{n} (negative) — use rng:{n}..0 instead",
                )
            return f"{rng.randint(0, n)}{_footer(seed)}"

        # Nothing matched.
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"unrecognised rng path: {body!r}",
            next="see get(id='rng:/help') for the full URI table",
        )

    # ------------------------------------------------------------------
    # Modes
    # ------------------------------------------------------------------

    def _coin_flip(self, seed: int | None) -> str:
        rng = _make_rng(seed)
        return f"{rng.randint(0, 1)}{_footer(seed)}"

    def _int_range(
        self, lo: int, hi: int, count: int,
        rng: _random.Random, seed: int | None,
    ) -> str:
        if lo > hi:
            lo, hi = hi, lo  # be permissive
        count = _clamp_count(count, kind="range")
        if count == 1:
            return f"{rng.randint(lo, hi)}{_footer(seed)}"
        values = [rng.randint(lo, hi) for _ in range(count)]
        return (
            f"[{', '.join(str(v) for v in values)}]"
            f" (n={count}, range=[{lo}, {hi}])"
            f"{_footer(seed)}"
        )

    def _float_range(
        self, body: str, rng: _random.Random, seed: int | None
    ) -> str:
        m = _FLOAT_RANGE_RE.match(body)
        if not m:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"rng:float/ requires <lo>..<hi>; got {body!r}",
                next="example: rng:float/0..1",
            )
        lo = float(m.group(1))
        hi = float(m.group(2))
        count = int(m.group(3)) if m.group(3) else 1
        count = _clamp_count(count, kind="float-range")
        if lo == hi:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="rng:float/ range must have lo < hi",
            )
        if lo > hi:
            lo, hi = hi, lo
        if count == 1:
            return f"{lo + (hi - lo) * rng.random():.17f}{_footer(seed)}"
        values = [lo + (hi - lo) * rng.random() for _ in range(count)]
        formatted = ", ".join(f"{v:.6f}" for v in values)
        return (
            f"[{formatted}] (n={count}, range=[{lo}, {hi}))"
            f"{_footer(seed)}"
        )

    def _dice(
        self, n: int, sides: int,
        rng: _random.Random, seed: int | None,
    ) -> str:
        if n < 1 or n > _MAX_LIST_LEN:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"dice count {n} outside [1, {_MAX_LIST_LEN}]",
            )
        if sides < 2:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"dice sides {sides} must be ≥ 2",
            )
        rolls = [rng.randint(1, sides) for _ in range(n)]
        total = sum(rolls)
        rolls_str = ", ".join(str(r) for r in rolls)
        return (
            f"🎲 {n}d{sides}\n"
            f"rolls: [{rolls_str}]\n"
            f"sum:   {total}"
            f"{_footer(seed)}"
        )

    def _choice(
        self, body: str, rng: _random.Random, seed: int | None,
    ) -> str:
        items = [s for s in body.split(",") if s]
        if not items:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="rng:choice/ requires a non-empty comma list",
                next="example: rng:choice/red,green,blue",
            )
        return f"{rng.choice(items)}{_footer(seed)}"

    def _shuffle(
        self, body: str, rng: _random.Random, seed: int | None,
    ) -> str:
        items = [s for s in body.split(",") if s]
        if not items:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="rng:shuffle/ requires a non-empty comma list",
                next="example: rng:shuffle/a,b,c,d",
            )
        rng.shuffle(items)
        return f"[{', '.join(items)}]{_footer(seed)}"

    def _bytes(self, body: str, seed: int | None) -> str:
        try:
            n = int(body)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"rng:bytes/ requires an integer; got {body!r}",
            ) from exc
        if n < 1 or n > _MAX_BYTES:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"rng:bytes/ length {n} outside [1, {_MAX_BYTES}]",
            )
        if seed is not None:
            log.warning("rng:bytes/ ignores ?seed= — bytes use CSPRNG")
        return f"{secrets.token_hex(n)}{_footer(None)}"

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _help(self) -> str:
        return (
            "# rng — random number generator\n\n"
            "Stateless, read-only, free.  Pure stdlib.  Coin flip default.\n\n"
            "## Integers (primary currency, both ends inclusive)\n\n"
            "- `get(id='rng:')`              — int [0, 1] (coin flip)\n"
            "- `get(id='rng:100')`           — int [0, 100]\n"
            "- `get(id='rng:1..6')`          — int [1, 6]\n"
            "- `get(id='rng:1..6x4')`        — 4 samples\n"
            "- `get(id='rng:3d6')`           — three six-sided dice + sum\n\n"
            "## Floats (opt-in, [lo, hi))\n\n"
            "- `get(id='rng:float')`         — float [0.0, 1.0)\n"
            "- `get(id='rng:float/0..1')`    — same, explicit\n"
            "- `get(id='rng:float/-1..1')`   — float in any range\n\n"
            "## Lists\n\n"
            "- `get(id='rng:choice/a,b,c')`     — uniform pick\n"
            "- `get(id='rng:shuffle/a,b,c,d')`  — random order\n\n"
            "## Crypto-grade\n\n"
            "- `get(id='rng:uuid')`          — UUID4 (CSPRNG-backed)\n"
            "- `get(id='rng:bytes/16')`      — N random bytes hex-encoded\n\n"
            "Crypto modes ignore `?seed=` — seeding CSPRNG is a footgun.\n\n"
            "## Reproducibility\n\n"
            "Pass `?seed=<int>` to make any non-crypto call deterministic:\n\n"
            "- `get(id='rng:?seed=42/3d6')`\n"
            "- `get(id='rng:?seed=7/1..6x10')`\n"
        )
