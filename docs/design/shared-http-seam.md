# Shared outbound-HTTP seam (`precis.utils.http`)

## Problem

Every kind that reaches the network open-coded the same three things:

```python
httpx = require_optional("httpx", extra="external")
with httpx.Client(timeout=30.0, headers={"User-Agent": "precis-mcp/1.0"}) as client:
    ...
except httpx.HTTPError as exc:
    raise Upstream(...)
```

Repeated across `handlers/web.py`, `handlers/news.py`, `handlers/wikipedia.py`,
`handlers/semanticscholar.py`, `handlers/math.py`, `handlers/perplexity.py`, and
`ingest/orcid.py`. Three things were duplicated and prone to drift:

1. The optional-dependency extra name (`"external"`) spelled out by hand.
2. The `User-Agent` header (sometimes `"precis-mcp/1.0"`, sometimes absent).
3. `follow_redirects=` — a **security-relevant** default. The SSRF guard in
   `precis.utils.safe_fetch` only works when the client does *not* auto-follow
   redirects (`safe_get` walks the chain itself, revalidating each hop). A
   client that defaulted to `follow_redirects=True` would let an agent-supplied
   URL redirect into a private/loopback/metadata address.

## What this is NOT

The bespoke per-kind **error messages** and `next=` hints are deliberate and
test-asserted (e.g. perplexity distinguishes `TimeoutException` from
`HTTPError`; web/news include the URL in the message). Those stay at the call
site — they are tuned, not duplicated. The seam centralizes only client
*construction*, not error handling.

## Design

`precis/utils/http.py`:

- `DEFAULT_USER_AGENT = "precis-mcp/1.0"`, `HTTPX_EXTRA = "external"`.
- `require_httpx()` — one place that names the `[external]` extra; used by
  callers that still need `httpx` for an `except httpx.HTTPError` clause.
- `http_client(*, timeout, headers=None, follow_redirects=False, user_agent=DEFAULT_USER_AGENT)`
  — constructs the client with the **secure `follow_redirects=False` default**
  and a merged UA header. `httpx` is imported lazily inside (the dep is
  optional), so importing this module never requires `[external]`.

## Deliberately left alone

- `handlers/youtube.py` watch-page scraper: wraps `import httpx` in a
  `try/except ImportError: return {}` to **degrade gracefully**. Routing it
  through `http_client` (which raises `Upstream` on a missing dep) would change
  that best-effort contract.
- `embedder.py` and `jobs/provenance_rw_sync.py`: use stdlib `urllib` on
  purpose to avoid pulling `httpx` into the torch-free / job paths. Out of scope.
- `ingest/crossref.py` and `ingest/semantic_scholar.py`: use library wrappers
  (`habanero`, `semanticscholar`), not raw `httpx`.

## Tests

`tests/utils/test_http.py` — UA default + override precedence, the
`follow_redirects=False` security default, header merging, `require_httpx`
identity, extra-name constant. Existing handler tests (web/news/wikipedia/
perplexity/math/orcid/semanticscholar) stay green unchanged, proving the error
messages and behavior are preserved.
