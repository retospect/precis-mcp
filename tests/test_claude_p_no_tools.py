"""Regression guard: ``call_claude_p`` must deny every tool.

The claude_p lane is one-shot text completion (quest_tick, figure,
finding_chase's taproot verdicts). Without a deny, newer CLI/model combos
answered by *calling harness tools* (ReportFindings, observed verbatim in
salvaged output) instead of emitting the requested text — an 88-100%
"unparseable model output" rate on every consumer (gr244061 / gr277659).
The bare ``"*"`` deny removes the tool definitions from the system prompt
entirely, so the model cannot even attempt one.
"""

from __future__ import annotations

from types import SimpleNamespace

import precis.utils.claude_p as claude_p
from precis.utils.claude_oauth import ENV_VAR


def test_call_claude_p_denies_every_tool(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "sk-ant-oat01-TEST")
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "claude")

    captured: dict = {}

    def _fake(args, *, binary, label, timeout_s, error_cls, env=None):
        captured["args"] = list(args)
        return SimpleNamespace(stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(claude_p, "run_claude", _fake)

    claude_p.call_claude_p("reply JSON {}")

    args = captured["args"]
    idx = args.index("--disallowedTools")
    assert args[idx + 1] == "*"
    # Must sit before the "--" end-of-options sentinel so the CLI parses it
    # as a flag, not as part of the positional prompt.
    assert idx < args.index("--")
