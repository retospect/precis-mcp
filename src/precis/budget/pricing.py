"""Token → dollar conversion for the OSS / local transports.

Authoritative cost is always the number a provider *returns* (claude reports
``total_cost_usd``; newer perplexity returns a ``cost`` block). This module is
the **fallback** for the one path that reports tokens but not dollars: the
OpenAI-compatible / OpenRouter backend driven through
:func:`precis.utils.llm.router.result_from_openai`.

The table is list prices in **USD per 1M tokens**, ``(input, output)``. It is
deliberately small — the ``ANTHROPIC`` backend (the default) reports real
dollars, so this only matters when a deployment flips
``PRECIS_LLM_BACKEND=openai`` at a hosted OSS endpoint. Unknown models return
``None`` (priced as free / unknown rather than guessed). Prefer OpenRouter's
own returned ``cost`` field where present; this table covers models that
don't report one.

Prices drift; treat entries as approximate and update as needed (see
``docs/design/budget-guardrails.md`` open question #2).
"""

from __future__ import annotations

#: ``model id → ($/1M input tokens, $/1M output tokens)``. Approximate list
#: prices. The local ``summarizer`` alias is intentionally absent — local
#: inference is priced as free (the ``LOCAL_SMALL`` band is ``free``).
PRICE_TABLE: dict[str, tuple[float, float]] = {
    # A few common hosted-OSS ids seen behind OpenRouter / DeepInfra. Extend
    # as a deployment pins concrete PRECIS_MODEL_* ids. Local aliases
    # (``summarizer``, ``qwen-heavy`` → the ``LOCAL_*`` free bands) are
    # deliberately absent: local inference is priced as free, so listing them
    # here would contradict their band.
    "deepseek-ai/DeepSeek-V3": (0.27, 1.10),
    "deepseek-ai/DeepSeek-R1": (0.55, 2.19),
    "meta-llama/Llama-3.3-70B-Instruct": (0.23, 0.40),
    "Qwen/Qwen2.5-72B-Instruct": (0.23, 0.40),
}


def cost_from_tokens(
    model: str,
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    """Estimate USD cost from a model id + token counts.

    Returns ``None`` when the model is unknown (priced as free / unknown) or
    when no token counts are available. Missing one side of the split is
    treated as ``0`` for that side rather than discarding the whole estimate.
    """
    rates = PRICE_TABLE.get(model)
    if rates is None:
        return None
    if prompt_tokens is None and completion_tokens is None:
        return None
    price_in, price_out = rates
    pin = (prompt_tokens or 0) / 1_000_000 * price_in
    pout = (completion_tokens or 0) / 1_000_000 * price_out
    return pin + pout


__all__ = ["PRICE_TABLE", "cost_from_tokens"]
