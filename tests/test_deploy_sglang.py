"""The sglang unit passes exactly one value to ``--served-model-name``.

The flag takes a single name; the old two-name default rendered a second
token, which argparse read as a positional and crash-looped the container
("unrecognized arguments: openai/gpt-oss-120b"). The default list now
holds the one name precis routes by (``openai/gpt-oss-120b``) and the unit
renders ``| first`` so a stray multi-item override cannot reintroduce the
crash.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import jinja2
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "sglang"
_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
_ENV.filters["to_json"] = json.dumps  # ansible filter; json.dumps suffices here


def _variables(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = yaml.safe_load(
        (_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    return {**values, "ansible_managed": "test", **overrides}


def _command(**overrides: Any) -> list[str]:
    rendered = _ENV.from_string(
        (_ROLE / "templates/sglang.service.j2").read_text(encoding="utf-8")
    ).render(**_variables(**overrides))
    # systemd joins continued lines before parsing ExecStart arguments.
    joined = rendered.replace("\\\n", " ")
    command = next(
        line for line in joined.splitlines() if line.startswith("ExecStart=")
    )
    return shlex.split(command.removeprefix("ExecStart="))


def _served_model_names(command: list[str]) -> list[str]:
    # Every token between --served-model-name and the next flag is a value.
    tail = command[command.index("--served-model-name") + 1 :]
    end = next(i for i, arg in enumerate(tail) if arg.startswith("--"))
    return tail[:end]


def test_default_serves_exactly_one_model_name() -> None:
    assert _served_model_names(_command()) == ["openai/gpt-oss-120b"]


def test_multi_name_override_still_serves_one() -> None:
    command = _command(
        sglang_served_model_names=["gpt-oss-120b", "openai/gpt-oss-120b"]
    )
    assert _served_model_names(command) == ["gpt-oss-120b"]
