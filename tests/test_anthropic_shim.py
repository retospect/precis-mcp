"""``scripts/anthropic-shim.py`` chat-mode translation, both directions.

gr500583: on SGLang's gpt-oss path ``/v1/messages`` answers every turn after
a tool result with a thinking block only, while ``/v1/chat/completions``
keeps calling tools — so the shim translates Messages to chat/completions.
These cover the pure translation: a tool round trip on the request side, a
streamed tool call on the response side, and stop_reason mapping. No server
is contacted.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

SHIM_PATH = Path(__file__).resolve().parents[1] / "scripts" / "anthropic-shim.py"


@pytest.fixture(scope="module")
def shim():
    loader = importlib.machinery.SourceFileLoader("anthropic_shim", str(SHIM_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# --- request side: Anthropic Messages -> chat/completions ---------------------


def test_system_folds_into_first_user_turn(shim):
    # On SGLang's gpt-oss path ANY system-role message stalls tool calling,
    # so the system prompt must ride the first user turn (gripe comment 1).
    body = {
        "system": "You are concise.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    msgs = shim.to_chat_body(body)["messages"]
    assert all(m["role"] != "system" for m in msgs)
    assert msgs == [{"role": "user", "content": "You are concise.\n\nhi"}]


def test_tool_round_trip_request(shim):
    body = {
        "system": [{"type": "text", "text": "Be brief."}],
        "messages": [
            {"role": "user", "content": "what is up"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "check status", "signature": "s"},
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get",
                        "input": {"kind": "skill", "id": "toc"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "the toc",
                    },
                    {"type": "text", "text": "thanks"},
                ],
            },
        ],
        "tools": [
            {
                "name": "get",
                "description": "fetch one",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
            }
        ],
        "max_tokens": 8000,
        "tool_choice": {"type": "auto"},
    }
    out = shim.to_chat_body(body)
    msgs = out["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]
    assert msgs[0]["content"] == "Be brief.\n\nwhat is up"
    # echoed thinking dropped; text + tool_calls survive
    assert msgs[1]["content"] == "Let me check."
    assert msgs[1]["tool_calls"] == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {
                "name": "get",
                "arguments": json.dumps({"kind": "skill", "id": "toc"}),
            },
        }
    ]
    # tool result becomes a tool message keyed by the same id
    assert msgs[2] == {"role": "tool", "tool_call_id": "toolu_1", "content": "the toc"}
    assert msgs[3] == {"role": "user", "content": "thanks"}
    assert out["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get",
                "description": "fetch one",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
            },
        }
    ]
    assert out["tool_choice"] == "auto"
    assert out["max_tokens"] == 8000


def test_tool_result_error_and_tool_choice(shim):
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "boom",
                        "is_error": True,
                    }
                ],
            }
        ],
        "tool_choice": {"type": "tool", "name": "get"},
    }
    out = shim.to_chat_body(body)
    assert out["messages"][0] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "[error] boom",
    }
    assert out["tool_choice"] == {"type": "function", "function": {"name": "get"}}
    body["tool_choice"] = {"type": "any"}
    assert shim.to_chat_body(body)["tool_choice"] == "required"


def test_stream_request_asks_for_usage(shim):
    out = shim.to_chat_body(
        {"messages": [{"role": "user", "content": "x"}], "stream": True}
    )
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}


# --- response side: chat/completions -> Anthropic Messages --------------------


def test_response_tool_call(shim):
    chat = {
        "id": "chatcmpl-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "reasoning_content": "need the toc",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {
                                "name": "get",
                                "arguments": '{"kind":"skill"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
    }
    msg = shim.to_anthropic_response(chat, "openai/gpt-oss-120b")
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert [b["type"] for b in msg["content"]] == ["thinking", "tool_use"]
    assert msg["content"][1] == {
        "type": "tool_use",
        "id": "call_9",
        "name": "get",
        "input": {"kind": "skill"},
    }
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 120, "output_tokens": 40}


@pytest.mark.parametrize(
    ("finish", "tools", "reason"),
    [
        ("stop", False, "end_turn"),
        ("stop", True, "tool_use"),  # tool blocks present -> tool_use either way
        ("tool_calls", True, "tool_use"),
        ("length", False, "max_tokens"),
        ("content_filter", False, "refusal"),
        (None, False, "end_turn"),
    ],
)
def test_stop_reason_mapping(shim, finish, tools, reason):
    msg = {
        "content": "x",
        "tool_calls": []
        if not tools
        else [{"id": "c", "function": {"name": "f", "arguments": "{}"}}],
    }
    chat = {"choices": [{"finish_reason": finish, "message": msg}]}
    assert shim.to_anthropic_response(chat, "m")["stop_reason"] == reason


def test_response_unparsed_arguments_kept(shim):
    chat = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "f", "arguments": "{not json"},
                        }
                    ]
                },
            }
        ]
    }
    block = shim.to_anthropic_response(chat, "m")["content"][0]
    assert block["type"] == "tool_use"
    assert block["input"] == {"_unparsed": "{not json"}


# --- streaming: chat/completions SSE -> Anthropic SSE --------------------------


def _events(shim, chunks):
    tr = shim.ChatStreamTranslator("openai/gpt-oss-120b")
    out = []
    for c in chunks:
        out.extend(tr.feed(c))
    out.extend(tr.close())
    return out


def test_streamed_tool_call(shim):
    chunks = [
        {"id": "chatcmpl-7", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {
            "choices": [
                {"index": 0, "delta": {"reasoning_content": "thinking about it"}}
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_3",
                                "type": "function",
                                "function": {"name": "search", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]
                    },
                }
            ]
        },
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"completion_tokens": 17},
        },
    ]
    ev = _events(shim, chunks)
    kinds = [(e, d.get("type")) for e, d in ev]
    assert kinds[0] == ("message_start", "message_start")
    assert ("content_block_start", "content_block_start") in kinds
    starts = [d for e, d in ev if e == "content_block_start"]
    assert [s["content_block"]["type"] for s in starts] == ["thinking", "tool_use"]
    tool_start = starts[1]["content_block"]
    assert tool_start["id"] == "call_3" and tool_start["name"] == "search"
    deltas = [d["delta"] for e, d in ev if e == "content_block_delta"]
    assert deltas[0] == {"type": "thinking_delta", "thinking": "thinking about it"}
    args = "".join(d["partial_json"] for d in deltas if d["type"] == "input_json_delta")
    assert json.loads(args) == {"q": "x"}
    md = [d for e, d in ev if e == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use"
    assert md["usage"] == {"output_tokens": 17}
    assert kinds[-1] == ("message_stop", "message_stop")


def test_streamed_text_reply(shim):
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "Hello"}}]},
        {
            "choices": [
                {"index": 0, "delta": {"content": " world"}, "finish_reason": "stop"}
            ]
        },
    ]
    ev = _events(shim, chunks)
    text = "".join(
        d["delta"]["text"]
        for e, d in ev
        if e == "content_block_delta" and d["delta"]["type"] == "text_delta"
    )
    assert text == "Hello world"
    md = [d for e, d in ev if e == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert ev[-1] == ("message_stop", {"type": "message_stop"})


def test_stream_two_tool_calls_get_two_blocks(shim):
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "a",
                                "function": {"name": "f", "arguments": "{}"},
                            },
                            {
                                "index": 1,
                                "id": "b",
                                "function": {"name": "g", "arguments": "{}"},
                            },
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    ev = _events(shim, chunks)
    starts = [d["content_block"] for e, d in ev if e == "content_block_start"]
    assert [(b["type"], b["id"], b["name"]) for b in starts] == [
        ("tool_use", "a", "f"),
        ("tool_use", "b", "g"),
    ]
    stops = [d["index"] for e, d in ev if e == "content_block_stop"]
    assert stops == [0, 1]


def test_stream_truncated_stops_cleanly(shim):
    # Upstream ended without finish_reason: still emit message_start..stop.
    ev = _events(shim, [{"choices": [{"index": 0, "delta": {"content": "partial"}}]}])
    kinds = [e for e, _ in ev]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    md = [d for e, d in ev if e == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn"


# --- shared pieces -------------------------------------------------------------


def test_count_tokens_estimate(shim):
    assert shim._estimate_tokens({"system": "", "messages": []}) == 1
    big = {"messages": [{"role": "user", "content": "x" * 400}]}
    assert shim._estimate_tokens(big) > 50
