# codereview: mypy per-module overrides (residual)

`warn_unused_ignores` shipped (365 dead suppressions deleted). Residual:
the global `ignore_missing_imports = true` is blunter than needed — the
genuinely untyped surface is ase, torch/sentence-transformers,
prompt-toolkit, discord, kokoro-onnx. Replace the global flag with
per-module `[[tool.mypy.overrides]]` so first-party `Any` leaks (the old
`cli/secret.py`/`cli/ingest.py` `[attr-defined]` cluster class) surface
instead of being absorbed. Mechanical but noisy; do as its own pass.
