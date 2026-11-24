"""Durable UTC-day token reservations shared by both grounding passes."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from precis.utils.llm.router import LlmRequest, LlmResult, route
from precis.workers._chase_llm import _verify_support_with_caveats

if TYPE_CHECKING:
    from precis.store.store import Store

_OUTPUT_TOKENS = 2048


class GroundingBudget:
    """Reserve before sending each attempt, including failover attempts.

    Prompt UTF-8 bytes plus framing and the completion cap provide a
    conservative charge independent of provider usage reporting. Committing
    the reservation separately preserves the daily ceiling across kills.
    """

    def __init__(self, store: Store, *, local: bool = False) -> None:
        self.store = store
        self.local = local
        try:
            self.ceiling = max(
                0, int(os.environ.get("PRECIS_INBOUND_GROUND_DAILY_TOKENS", "250000"))
            )
        except ValueError:
            self.ceiling = 250_000
        self.tokens = 0
        self.exhausted = self.ceiling == 0

    def _reserve(self, tokens: int) -> bool:
        if tokens > self.ceiling:
            self.exhausted = True
            return False
        with self.store.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO inbound_ground_budget (day_utc, tokens)
                VALUES ((clock_timestamp() AT TIME ZONE 'UTC')::date, %s)
                ON CONFLICT (day_utc) DO UPDATE
                    SET tokens = inbound_ground_budget.tokens + EXCLUDED.tokens
                    WHERE inbound_ground_budget.tokens + EXCLUDED.tokens <= %s
                RETURNING tokens
                """,
                (tokens, self.ceiling),
            ).fetchone()
            conn.commit()
        if row is None:
            self.exhausted = True
            return False
        self.tokens += tokens
        return True

    def _dispatch(self, request: LlmRequest) -> LlmResult:
        tokens = len(request.prompt.encode("utf-8")) + 256 + _OUTPUT_TOKENS
        return route(
            replace(
                request,
                placement="local" if self.local else request.placement,
                max_tokens=_OUTPUT_TOKENS,
                admit_attempt=lambda: self._reserve(tokens),
            )
        )

    def verify(self, **kwargs: Any) -> dict[str, Any] | None:
        return _verify_support_with_caveats(**kwargs, dispatch=self._dispatch)
