"""Render the vLLM service variants and probe the address actually published."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "vllm"
_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)


def _variables(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = yaml.safe_load(
        (_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    return {**values, "ansible_managed": "test", **overrides}


def _command(**overrides: Any) -> list[str]:
    rendered = _ENV.from_string(
        (_ROLE / "templates/vllm.service.j2").read_text(encoding="utf-8")
    ).render(**_variables(**overrides))
    # systemd joins continued lines before parsing ExecStart arguments.
    joined = rendered.replace("\\\n", " ")
    command = next(
        line for line in joined.splitlines() if line.startswith("ExecStart=")
    )
    return shlex.split(command.removeprefix("ExecStart="))


def test_gpt_oss_service_mounts_offline_tokenizers() -> None:
    command = _command()
    assert "/srv/models/gpt-oss-120b:/model:ro" in command
    assert "/srv/models/tiktoken:/tiktoken:ro" in command
    assert "TIKTOKEN_ENCODINGS_BASE=/tiktoken" in command
    assert command[command.index("--tool-call-parser") + 1] == "openai"
    assert command[command.index("--max-num-seqs") + 1] == "64"


def test_nemotron_service_omits_tokenizers_and_preserves_extra_arguments() -> None:
    command = _command(
        vllm_model_dir="/srv/models/nemotron-3-super-nvfp4",
        vllm_served_model_name="nemotron-3-super",
        vllm_tool_call_parser="qwen3_xml",
        vllm_tiktoken_dir="",
        vllm_extra_args=["--trust-remote-code", "--dtype", "auto"],
    )
    assert "/srv/models/nemotron-3-super-nvfp4:/model:ro" in command
    assert not any("tiktoken" in argument.lower() for argument in command)
    assert command[command.index("--served-model-name") + 1] == "nemotron-3-super"
    assert command[-5:] == [
        "--tool-call-parser",
        "qwen3_xml",
        "--trust-remote-code",
        "--dtype",
        "auto",
    ]


@pytest.mark.parametrize(
    ("bind", "mapping", "probe"),
    [
        ("0.0.0.0", "0.0.0.0:8000:8000", "127.0.0.1"),
        ("203.0.113.10", "203.0.113.10:8000:8000", "203.0.113.10"),
        ("::", "[::]:8000:8000", "[::1]"),
        ("2001:db8::10", "[2001:db8::10]:8000:8000", "[2001:db8::10]"),
    ],
)
def test_health_probe_matches_published_address(
    bind: str, mapping: str, probe: str
) -> None:
    command = _command(vllm_bind_addr=bind)
    assert command[command.index("-p") + 1] == mapping
    tasks = yaml.safe_load((_ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    task = next(task for task in tasks if "ansible.builtin.uri" in task)
    variables = _variables(vllm_bind_addr=bind)
    variables["_vllm_health_addr"] = _ENV.from_string(
        task["vars"]["_vllm_health_addr"]
    ).render(**variables)
    url = _ENV.from_string(task["ansible.builtin.uri"]["url"]).render(**variables)
    assert url == f"http://{probe}:8000/health"
