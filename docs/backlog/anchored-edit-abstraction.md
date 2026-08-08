# Anchored-edit region resolution — a design call, not an extraction

Corrected premise (2026-07-23): only plaintext.py and python.py implement
`_put_anchored` (markdown/tex inherit PlaintextHandler's; draft is
chunk-native), and the shared find=/text= validation is already factored into
`plaintext._require_find_and_text`. A genuine EditableFileHandler base means
designing a shared region-resolution abstraction across paragraph-blocks vs
AST-symbol-ranges (+ qualname-drop/ruff gates) — an Opus-tier
core-abstraction call, not mechanical. Owner
`src/precis/handlers/plaintext.py`, `src/precis/handlers/python.py`.
