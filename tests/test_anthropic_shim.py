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
    # Non-gpt-oss models keep the gr500583 fold: the system prompt rides the
    # first user turn.
    body = {
        "system": "You are concise.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    msgs = shim.to_chat_body(body, "auto")["messages"]
    assert all(m["role"] != "system" for m in msgs)
    assert msgs == [{"role": "user", "content": "You are concise.\n\nhi"}]


def test_gpt_oss_system_goes_out_as_system_message(shim):
    # castor probe 2026-10-22: a system message called a tool on turn 1 in
    # 4/5 requests vs 2/5 folded, so gpt-oss defaults to a system message.
    body = {
        "model": "openai/gpt-oss-120b",
        "system": "You are concise.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert shim.to_chat_body(body, "auto")["messages"] == [
        {"role": "system", "content": "You are concise."},
        {"role": "user", "content": "hi"},
    ]
    # The flag forces the fold back on.
    assert shim.to_chat_body(body, "fold")["messages"] == [
        {"role": "user", "content": "You are concise.\n\nhi"}
    ]


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


# --- dropped tool calls: detect and retry ----------------------------------------
#
# gpt-oss on SGLang sometimes writes its tool call on the analysis channel;
# the parser drops it and the turn ends finish "stop" on <|call|> (200012)
# with tool_calls null. The shim re-sends such turns, then errors out.


def _chat(finish, message, matched_stop=None):
    return {
        "choices": [
            {"finish_reason": finish, "matched_stop": matched_stop, "message": message}
        ]
    }


DROPPED = _chat("stop", {"reasoning_content": "let me grep", "content": None}, 200012)
TOOL = _chat(
    "tool_calls",
    {
        "reasoning_content": "let me grep",
        "tool_calls": [
            {"id": "c1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}
        ],
    },
)
TEXT = _chat("stop", {"content": "All done."}, 200002)

DROPPED_STREAM = [
    {"choices": [{"index": 0, "delta": {"reasoning_content": "let me grep"}}]},
    {
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": "stop", "matched_stop": 200012}
        ]
    },
]
TOOL_STREAM = [
    {"choices": [{"index": 0, "delta": {"reasoning_content": "let me grep"}}]},
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {
                                "name": "Bash",
                                "arguments": '{"command":"ls"}',
                            },
                        }
                    ]
                },
            }
        ]
    },
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
]
TEXT_STREAM = [
    {"choices": [{"index": 0, "delta": {"reasoning_content": "easy"}}]},
    {"choices": [{"index": 0, "delta": {"content": "All done."}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
]


class _Upstream:
    """Fake upstream: hands out the queued replies in order, counting calls."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


@pytest.mark.parametrize(
    ("finish", "matched", "content", "reasoning", "tools", "dropped"),
    [
        ("stop", 200012, "", True, False, True),
        (
            "stop",
            None,
            "<|start|>assistant to=functions.Bash<|call|>",
            True,
            False,
            True,
        ),
        ("stop", None, "", True, False, True),  # reasoning only
        ("stop", 200002, "All done.", True, False, False),
        ("stop", 200012, "", True, True, False),  # tool call made it through
        ("tool_calls", None, "", True, True, False),
        ("length", None, "", True, False, False),
        ("stop", None, "", False, False, False),  # empty but no reasoning
    ],
)
def test_dropped_call_reason(shim, finish, matched, content, reasoning, tools, dropped):
    reason = shim.dropped_call_reason(finish, matched, content, reasoning, tools)
    assert (reason is not None) == dropped


def test_complete_retry_recovers_tool_call(shim):
    upstream, logged = _Upstream(DROPPED, TOOL), []
    msg = shim.complete_with_retry(upstream, "m", 3, lambda *a: logged.append(a))
    assert upstream.calls == 2
    assert msg["stop_reason"] == "tool_use"
    assert [b["type"] for b in msg["content"]] == ["thinking", "tool_use"]
    assert len(logged) == 1
    assert logged[0][0] == "retry" and logged[0][1].startswith(
        "1/3: matched_stop 200012"
    )


def test_complete_retry_exhausted_raises(shim):
    upstream, logged = _Upstream(DROPPED), []
    with pytest.raises(shim.DroppedToolCall, match="4 attempts"):
        shim.complete_with_retry(upstream, "m", 3, lambda *a: logged.append(a))
    assert upstream.calls == 4
    assert [(e, d[:3]) for e, d in logged] == [("retry", f"{n}/3") for n in (1, 2, 3)]


@pytest.mark.parametrize("chat", [TEXT, TOOL])
def test_complete_normal_turn_untouched(shim, chat):
    upstream, logged = _Upstream(chat), []
    msg = shim.complete_with_retry(upstream, "m", 3, lambda *a: logged.append(a))
    assert upstream.calls == 1 and logged == []
    plain = shim.to_anthropic_response(chat, "m")
    # identical apart from the random message id
    assert {**msg, "id": ""} == {**plain, "id": ""}


def test_stream_retry_recovers_tool_call(shim):
    upstream, logged = _Upstream(DROPPED_STREAM, TOOL_STREAM), []
    ev = list(shim.stream_with_retry(upstream, "m", 3, lambda *a: logged.append(a)))
    assert upstream.calls == 2 and len(logged) == 1
    # the dropped attempt never reaches the client: one message, one tool_use
    assert [e for e, _ in ev].count("message_start") == 1
    starts = [d["content_block"]["type"] for e, d in ev if e == "content_block_start"]
    assert starts == ["thinking", "tool_use"]
    md = [d for e, d in ev if e == "message_delta"]
    assert [d["delta"]["stop_reason"] for d in md] == ["tool_use"]


def test_stream_leaked_call_in_content_is_retried(shim):
    leaked = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "open it"}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": "<|start|>assistant to=functions.Bash<|call|>"
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    ]
    upstream = _Upstream(leaked, TOOL_STREAM)
    ev = list(shim.stream_with_retry(upstream, "m", 3, lambda *a: None))
    assert upstream.calls == 2
    assert not any(d.get("delta", {}).get("type") == "text_delta" for _, d in ev)


def test_stream_retry_exhausted_raises_before_any_event(shim):
    upstream, logged = _Upstream(DROPPED_STREAM), []
    events = shim.stream_with_retry(upstream, "m", 3, lambda *a: logged.append(a))
    with pytest.raises(shim.DroppedToolCall):
        next(events)
    assert upstream.calls == 4 and len(logged) == 3


@pytest.mark.parametrize("chunks", [TEXT_STREAM, TOOL_STREAM])
def test_stream_normal_turn_untouched(shim, chunks):
    upstream, logged = _Upstream(chunks), []
    ev = list(shim.stream_with_retry(upstream, "m", 3, lambda *a: logged.append(a)))
    assert upstream.calls == 1 and logged == []
    plain = _events(shim, chunks)
    # identical apart from the per-translator random message id
    assert [(e, d.get("type")) for e, d in ev] == [(e, d.get("type")) for e, d in plain]
    assert ev[1:] == plain[1:]


# --- salvage: dropped call read back from raw harmony ----------------------------
#
# castor 2026-10-22, raw mode (separate_reasoning false, skip_special_tokens
# false, no_stop_trim): the dropped call came back verbatim in content, with
# the tool name lowercased.

TOOLS = ["Bash", "Read", "mcp__precis__get"]


def _raw(content, tool_calls=None):
    return {
        "id": "raw-1",
        "choices": [
            {
                "finish_reason": "stop",
                "matched_stop": 200012,
                "message": {"content": content, "tool_calls": tool_calls},
            }
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 30},
    }


RAW_DROPPED = _raw(
    "<|channel|>analysis<|message|>We need to run tests to see failure.<|end|>"
    "<|start|>assistant<|channel|>analysis to=functions.bash code<|message|>"
    '{"command":"ls -R"}<|call|>'
)


def _salvaged_call(chat):
    msg = chat["choices"][0]["message"]
    (tc,) = msg["tool_calls"]
    return tc["function"]["name"], json.loads(tc["function"]["arguments"])


def test_salvage_maps_lowercase_name_to_canonical(shim):
    chat, how = shim.salvage_call(RAW_DROPPED, TOOLS)
    assert chat is not None, how
    assert _salvaged_call(chat) == ("Bash", {"command": "ls -R"})
    assert chat["choices"][0]["finish_reason"] == "tool_calls"
    assert chat["choices"][0]["message"]["reasoning_content"] == (
        "We need to run tests to see failure."
    )
    assert "bash -> Bash" in how


def test_salvage_reads_logprob_tokens_when_content_is_swallowed(shim):
    # With Claude Code's full tool set SGLang returned content '' and
    # reasoning '' for ~100 completion tokens; the logprob tokens kept them.
    text = (
        "<|channel|>analysis<|message|>Let's read it.<|end|><|start|>assistant"
        "<|channel|>commentary to=functions.read code<|message|>"
        '{\n  "file_path": "/src/bib_parse.py"\n}<|call|>'
    )
    raw = _raw("")
    raw["choices"][0]["logprobs"] = {
        "content": [{"token": text[i : i + 7]} for i in range(0, len(text), 7)]
    }
    chat, how = shim.salvage_call(raw, TOOLS)
    assert chat is not None, how
    assert _salvaged_call(chat) == ("Read", {"file_path": "/src/bib_parse.py"})
    assert chat["choices"][0]["message"]["reasoning_content"] == "Let's read it."


def test_salvage_takes_the_last_call(shim):
    raw = _raw(
        "<|channel|>analysis to=functions.Read code<|message|>"
        '{"file_path":"/a"}<|call|>'
        "<|start|>assistant<|channel|>commentary to=functions.Bash "
        '<|constrain|>json<|message|>{"command":"pwd"}<|call|>'
    )
    chat, _ = shim.salvage_call(raw, TOOLS)
    assert _salvaged_call(chat) == ("Bash", {"command": "pwd"})


def test_salvage_unknown_tool_falls_through(shim):
    raw = _raw(
        "<|channel|>analysis to=functions.apply_patch code<|message|>"
        '{"patch":"x"}<|call|>'
    )
    chat, how = shim.salvage_call(raw, TOOLS)
    assert chat is None and "apply_patch" in how


@pytest.mark.parametrize("args", ['{"command": "ls"', '["ls"]', "not json"])
def test_salvage_bad_json_falls_through(shim, args):
    raw = _raw(f"<|channel|>analysis to=functions.Bash code<|message|>{args}<|call|>")
    chat, how = shim.salvage_call(raw, TOOLS)
    assert chat is None and "JSON object" in how


def test_salvage_no_call_falls_through(shim):
    chat, how = shim.salvage_call(
        _raw("<|channel|>final<|message|>Done.<|return|>"), TOOLS
    )
    assert chat is None and "no harmony tool call" in how


def test_salvage_uses_parsed_tool_calls_with_known_names(shim):
    raw = _raw(
        "<|channel|>analysis<|message|>look<|end|>",
        [{"id": "c", "function": {"name": "read", "arguments": '{"file_path":"/x"}'}}],
    )
    chat, _ = shim.salvage_call(raw, TOOLS)
    assert _salvaged_call(chat) == ("Read", {"file_path": "/x"})
    bad = _raw("", [{"id": "c", "function": {"name": "Edit", "arguments": "{}"}}])
    assert shim.salvage_call(bad, TOOLS)[0] is None


def test_complete_salvage_recovers_tool_use(shim):
    upstream, raw, logged = _Upstream(DROPPED), _Upstream(RAW_DROPPED), []
    msg = shim.complete_with_retry(
        upstream, "m", 3, lambda *a: logged.append(a), raw, TOOLS
    )
    assert upstream.calls == 1 and raw.calls == 1
    assert msg["stop_reason"] == "tool_use"
    tool = [b for b in msg["content"] if b["type"] == "tool_use"]
    assert [(b["name"], b["input"]) for b in tool] == [("Bash", {"command": "ls -R"})]
    assert [e for e, _ in logged] == ["retry", "salvaged"]


def test_complete_salvage_refused_then_exhausted(shim):
    bad = _raw("<|channel|>analysis to=functions.nope code<|message|>{}<|call|>")
    upstream, raw, logged = _Upstream(DROPPED), _Upstream(bad), []
    with pytest.raises(shim.DroppedToolCall, match="unknown tool"):
        shim.complete_with_retry(
            upstream, "m", 3, lambda *a: logged.append(a), raw, TOOLS
        )
    # retries go to the raw request, not the normal one
    assert upstream.calls == 1 and raw.calls == 3
    assert [e for e, _ in logged] == ["retry", "salvage_refused"] * 3


def test_stream_salvage_recovers_tool_use(shim):
    upstream, raw, logged = _Upstream(DROPPED_STREAM), _Upstream(RAW_DROPPED), []
    ev = list(
        shim.stream_with_retry(
            upstream, "m", 3, lambda *a: logged.append(a), raw, TOOLS
        )
    )
    assert upstream.calls == 1 and raw.calls == 1
    assert [e for e, _ in ev].count("message_start") == 1
    starts = [d["content_block"] for e, d in ev if e == "content_block_start"]
    assert [b["type"] for b in starts] == ["thinking", "tool_use"]
    assert starts[1]["name"] == "Bash"
    args = "".join(
        d["delta"]["partial_json"]
        for e, d in ev
        if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(args) == {"command": "ls -R"}
    md = [d for e, d in ev if e == "message_delta"]
    assert [d["delta"]["stop_reason"] for d in md] == ["tool_use"]
    assert ev[-1][0] == "message_stop"


@pytest.mark.parametrize("chunks", [TEXT_STREAM, TOOL_STREAM])
def test_stream_salvage_leaves_normal_turns_alone(shim, chunks):
    upstream, raw = _Upstream(chunks), _Upstream(RAW_DROPPED)
    list(shim.stream_with_retry(upstream, "m", 3, lambda *a: None, raw, TOOLS))
    assert upstream.calls == 1 and raw.calls == 0


@pytest.mark.parametrize("chat", [TEXT, TOOL])
def test_complete_salvage_leaves_normal_turns_alone(shim, chat):
    upstream, raw = _Upstream(chat), _Upstream(RAW_DROPPED)
    shim.complete_with_retry(upstream, "m", 3, lambda *a: None, raw, TOOLS)
    assert upstream.calls == 1 and raw.calls == 0


def test_salvage_default_on_for_gpt_oss_only(shim):
    assert shim.salvage_on("openai/gpt-oss-120b", "auto")
    assert not shim.salvage_on("qwen3", "auto")
    assert not shim.salvage_on("openai/gpt-oss-120b", "off")
    assert shim.salvage_on("qwen3", "on")


# --- shared pieces -------------------------------------------------------------


def test_count_tokens_estimate(shim):
    assert shim._estimate_tokens({"system": "", "messages": []}) == 1
    big = {"messages": [{"role": "user", "content": "x" * 400}]}
    assert shim._estimate_tokens(big) > 50
