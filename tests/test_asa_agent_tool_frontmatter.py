"""gr761752: asa's synced specialist agents must declare their real tool set.

The asa_bot role prepends YAML frontmatter onto every grimoire
``agents/*.md`` it syncs into ``~/.claude/agents/``. With no ``tools``
field, Claude Code's Agent-tool catalog reports each profile as
"Tools: *" / "All tools" — a dispatching agent then believes a coder or
general-purpose subagent can run ``Bash``/``Edit``/``Write`` and burns
turns on dispatches the session-level deny list silently drops (no TTY
to approve, no error surfaced). Three such dispatches were the report.

The declared list must equal the deployed ``claude_settings.json.j2``
``permissions.allow`` ceiling so the catalog advertises exactly the
tools a subagent can really call — a narrower declared set would hide
capability, a wider one would repeat the original lie.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "asa_bot"


def _find_task(name: str) -> dict[str, Any]:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    stack = list(tasks)
    while stack:
        task = stack.pop()
        if not isinstance(task, dict):
            continue
        if task.get("name") == name:
            return task
        for key in ("block", "rescue", "always"):
            nested = task.get(key)
            if isinstance(nested, list):
                stack.extend(nested)
    raise AssertionError(f"{name!r} task not found")


def _declared_tools(content: str) -> set[str]:
    match = re.search(r"^tools:\s*(?P<tools>.+?)\s*$", content, re.MULTILINE)
    assert match, (
        "agent frontmatter declares no tools — the Agent-tool catalog "
        "reports 'Tools: *' while the session denies Bash/Edit/Write"
    )
    return {tool.strip() for tool in match["tools"].split(",")}


def _deployed_ceiling() -> set[str]:
    return set(
        json.loads(
            (_ROLE / "templates" / "claude_settings.json.j2").read_text(
                encoding="utf-8"
            )
        )["permissions"]["allow"]
    )


def test_synced_agent_frontmatter_declares_the_deployed_tool_ceiling() -> None:
    content = _find_task("Copy specialist subagent definition")["ansible.builtin.copy"][
        "content"
    ]
    assert _declared_tools(content) == _deployed_ceiling()


def test_shadowed_builtin_agents_declare_the_deployed_tool_ceiling() -> None:
    # Built-ins keep their own manifests — the grimoire sync cannot
    # relabel them — so the two bare "Tools: *" catch-alls are shadowed
    # by same-named files. general-purpose is the Agent-tool default
    # fallback and one of the two types the gripe named.
    task = _find_task("Shadow built-in agents that still over-advertise tools")
    copy = task["ansible.builtin.copy"]
    assert _declared_tools(copy["content"]) == _deployed_ceiling()
    assert {item["name"] for item in task["loop"]} == {
        "general-purpose",
        "claude",
    }
