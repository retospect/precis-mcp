# /factory POST routes have no auth (gripe 171512)

No auth middleware on any /factory write; sharpest for `POST
/factory/llm/chain`, which can route prod traffic and cost through OpenRouter
now that the base URL + key are deployed. Mitigated by tailnet-only exposure
(*.ts.net) — tailnet-trust is currently accepted; gate it or accept
consciously. Owner `src/precis_web/routes/factory.py` / `app.py`.

Duplicate field report gr171512 folded here.
