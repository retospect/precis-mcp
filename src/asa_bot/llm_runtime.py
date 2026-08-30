"""Shared in-process precis runtime warm-up for the chat bridges.

Both bridges (Discord's ``local`` lane, asa-slack's every turn) route
through ``precis.utils.llm.router.route()``, whose chain resolution and
OSS transports read process-global stores that ``build_runtime`` binds as a
side effect — and read them *dark* (fall back to the default claude chain /
an empty key) when nothing has bound a store yet. A bridge process never
binds one on its own: its precis access goes through the MCP subprocess.
So every bridge must call :func:`warm_runtime` before its first dispatch,
on a worker thread (the build opens DB connections).
"""

from __future__ import annotations


def warm_runtime() -> None:
    """Bind the in-process precis runtime before a chain dispatch.

    ``route``'s chain-override read (``live_config``), the local-serving
    slot lookup, and the hosted-OSS vault-key resolution all read
    process-global stores that ``build_runtime`` binds as a side effect
    (``adopt_process_store`` / the budget-meter store) — and the OSS tool
    loop only builds that runtime *after* it has already resolved its
    endpoint + key. Without this warm-up, a chain dispatch in a fresh
    bridge process resolves the default (claude) chain and sees no local
    endpoint / an empty API key. The loop reuses this exact cached runtime
    for its in-process verb execution, so this costs nothing after the
    first call.
    """
    from precis.tools.core import _get_runtime

    _get_runtime()
