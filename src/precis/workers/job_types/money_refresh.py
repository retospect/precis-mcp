"""``money_refresh`` job_type — refill the FX/CPI caches, deterministically.

Thin wrapper that lets the weekly currency-rate refresh run as a precis
*job* instead of an OS cron / launchd timer. Like ``news_poll`` it is
deterministic, in-process work (no claude) registered with a plugin
``dispatch`` so ``claude_inproc`` runs it directly.

The intended driver is a recurring (``meta.schedule`` set) todo
(``meta.schedule={'cron':'0 6 * * 1'}`` — Monday 06:00Z,
``meta.executor='claude_inproc'``, ``meta.job_type='money_refresh'``),
created in prod by the coordinator at deploy, not by the build slice:
the type ships registered and does not run until that ops step.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "items": {"enum": ["ecb", "worldbank"]},
            "minItems": 1,
            "uniqueItems": True,
            "description": "Caches to refresh (default: both).",
        }
    },
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any, fetch: Any = None) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job.

    ``fetch`` is the ``money_refresh`` injection seam — the executor
    never passes it; tests do.
    """
    from precis.workers.money_refresh import run_money_refresh

    params = (ctx.meta or {}).get("params") or {}
    try:
        counts = run_money_refresh(
            ctx.store, sources=params.get("sources"), fetch=fetch
        )
    except Exception as exc:  # one bad fetch shouldn't wedge the cadence
        log.warning("money_refresh job: pass raised", exc_info=True)
        ctx.record_failure(f"money_refresh: pass raised: {exc}")
        return

    ctx.append_chunk(
        "job_summary",
        "money_refresh: " + ", ".join(f"{name} {n} rows" for name, n in counts.items()),
    )
    ctx.set_meta(**{f"{name}_rows": n for name, n in counts.items()})


SPEC = JobTypeSpec(
    name="money_refresh",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description="Refresh the fx_rates / price_index caches from the ECB "
    "and World Bank (weekly recurring todo).",
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
