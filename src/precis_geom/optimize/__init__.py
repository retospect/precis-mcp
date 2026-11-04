"""DB-free search helpers over caller-owned recipes and objectives.

``anneal(moves, energy, schedule, rng)`` implements Metropolis search without
knowing geometry or physics. Moves own independent snapshots and restoration
so a rejected proposal can undo all recipe and solver state together. The
caller supplies both proposal scaling and randomness; the helper passes
``T/T0`` to proposals and never uses global random state. The best snapshot
is restored on completion, since the last accepted state can be worse.
"""

from .anneal import AnnealResult, Moves, RandomSource, anneal, geometric_schedule

__all__ = ["AnnealResult", "Moves", "RandomSource", "anneal", "geometric_schedule"]
