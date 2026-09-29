# Embedder wedged in "warming" — /healthz lies, /readyz tells

**Symptom.** Every `POST /embed` against the native `com.precis.embedder`
launchd service (`precis serve-embeddings`, bge-m3, loopback `:8181`) returns
500 with "embedder warming — bge-m3 weights are still loading; retry in ~30
seconds", for hours or days. Search across the local Docker stack degrades to
lexical or blocks (worker and watch embed against it via
`host.docker.internal:8181`); the first `search(kind='skill')` of a session
can stall the MCP server for ~20 min because the skill-index build embeds
serially instead of fast-failing to lexical.

**Tell a wedge from a real warm-up** (a real warm-up is minutes, not hours):

- RSS of the process is ~30 MB — bge-m3 is ~2 GB, so the model never loaded.
- `sample <pid>` shows one thread in `poll` (the accept loop); the
  `embedder-warm` daemon thread is gone. No libtorch / safetensors in
  `lsof -p <pid>`.
- `~/Library/Logs/precis-embedder.log` fills with identical tracebacks.
- `/healthz` still answers `ok` — it is liveness only. `/readyz` answers 503,
  which is correct, but nothing acts on it: launchd `KeepAlive` restarts only
  on process *exit*, and a wedged process never exits.

**Root cause, historical.** The warm thread cleared the warming gate by
calling `embed("warmup")`, which is itself gated by `_raise_if_warming()`; it
fast-failed on the gate it was meant to open, the `except` swallowed it and
`_ready` was never set. Fixed 2026-06 in
`src/precis/embedder_service.py::_warm` (it now calls the embedder's own
`warmup()`). The wedge lived on for 13 days afterwards because the service
runs editable from the working tree and was never restarted — a fix on disk
is not a fix in the process.

**Recovery.**

```
launchctl kickstart -k gui/$(id -u)/com.precis.embedder
curl -s -d '{"texts":["x"]}' -H 'content-type: application/json' http://127.0.0.1:8181/embed | head -c 200
```

Expect a 1024-dim vector, not the warming 500. Because the service runs
editable from the main checkout, the restart deploys whatever is in the
working tree. Watch `/readyz` (`{"status": "ready"}`), never `/healthz`; the
per-host log table is in `cluster-logs.md`.

**Related.** `search_embed_guard` (search must degrade to lexical, not 500);
`restart-worker-and-watch.md` for the launchd restart idiom.
