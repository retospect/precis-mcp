"""LLM call layer for the classifier — one JSON classification per call.

Two backends, one interface:
  * local  (default) — the cheap litellm/qwen path `llm_summarize` uses
    (OpenAI /chat/completions at the loopback proxy). Scales to the whole
    corpus; this is what `classify-papers` / the chunk_tag pass will use.
  * claude:<model>   — `call_claude_p` (Anthropic CLI), for a quality
    reference point when auditing the local model against the gold set.

Every axis prompt already asks for `{"value","confidence","rationale"}`;
we parse the last JSON object out of the model's text and return it as a
dict (or None on failure, so the caller can count llm-errors).
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    # Fast path: whole string is JSON.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Else: the outermost {...} span (handles prose or ```json fences).
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


# ---- litellm proxy (local qwen* AND proxied claude-*/deepseek*/…) ------
# One HTTP surface for every model the proxy exposes (GET /v1/models):
# qwen / qwen-heavy / summarizer / deepseek-v4-* / gemini-deep run locally,
# claude-haiku-4-5 / claude-sonnet / claude-opus are proxied to Anthropic.
# This sidesteps the `claude -p` CLI (which needs the worker's OAuth env).

_clients: dict[str, object] = {}


def _get_local_client(model: str | None):
    key = model or os.environ.get("PRECIS_SUMMARIZE_MODEL") or "summarizer"
    if key not in _clients:
        from precis.workers.llm_summarize import LlmClient, LlmConfig

        cfg = LlmConfig(
            enabled=True,
            url=os.environ.get("PRECIS_SUMMARIZE_LLM_URL", "http://127.0.0.1:4000/v1"),
            model=key,
            api_key=os.environ.get("PRECIS_SUMMARIZE_LLM_KEY", "dummy"),
            max_tokens=int(os.environ.get("PRECIS_CLASSIFY_MAX_TOKENS", "220")),
            timeout=float(os.environ.get("PRECIS_CLASSIFY_TIMEOUT", "180")),
            concurrency=1,
        )
        _clients[key] = LlmClient(cfg)
    return _clients[key]


def _classify_local(prompt: str, model: str | None) -> dict | None:
    client = _get_local_client(model)
    try:
        out = client.complete(
            [
                {
                    "role": "system",
                    "content": "You are a precise single-label classifier. "
                    "Reply with ONLY the requested JSON object, no prose.",
                },
                {"role": "user", "content": prompt},
            ]
        )
    except Exception:
        return None
    return _extract_json(out.text)


# ---- claude (reference) -----------------------------------------------


def _classify_claude(prompt: str, model: str) -> dict | None:
    from precis.utils.claude_p import ClaudePError, call_claude_p

    try:
        res = call_claude_p(
            prompt,
            model=model,
            max_usd=float(os.environ.get("PRECIS_CLASSIFY_MAX_USD", "0.03")),
            timeout_s=90,
        )
    except ClaudePError:
        return None
    except Exception:
        return None
    return res.data


# ---- dispatch ---------------------------------------------------------


def classify_one(prompt: str, model: str | None = None) -> dict | None:
    """Return the parsed {"value",...} dict, or None on any failure.

    Every model (qwen*, claude-*, deepseek*, …) goes through the litellm
    proxy over HTTP. Use the `cli:<model>` prefix to force the `claude -p`
    subprocess path instead (needs the worker OAuth env).
    """
    if model and model.startswith("cli:"):
        return _classify_claude(prompt, model[4:])
    return _classify_local(prompt, model)


def classify_batch(
    prompts: list[str], *, model: str | None = None, concurrency: int = 4
) -> list[dict | None]:
    if concurrency <= 1:
        return [classify_one(p, model) for p in prompts]
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        return list(ex.map(lambda p: classify_one(p, model), prompts))
