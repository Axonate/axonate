"""Unit tests for Anthropic<->OpenAI translation. No Docker, no network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "router"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth"))
from router import (  # noqa: E402
    anthropic_to_openai, openai_to_anthropic, openai_sse_to_anthropic_events, _text_from_content,
)


# ---- request: Anthropic -> OpenAI ----

def test_system_string_becomes_system_message():
    out = anthropic_to_openai({"model": "claude", "max_tokens": 50, "system": "be terse",
                               "messages": [{"role": "user", "content": "hi"}]})
    assert out["messages"][0] == {"role": "system", "content": "be terse"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["max_tokens"] == 50 and out["model"] == "claude"


def test_system_block_list_flattened():
    out = anthropic_to_openai({"model": "claude", "system": [{"type": "text", "text": "A"},
                                                             {"type": "text", "text": "B"}],
                               "messages": []})
    assert out["messages"][0]["content"] == "AB"


def test_content_blocks_flatten_and_drop_nontext():
    c = [{"type": "text", "text": "hello "}, {"type": "image", "source": {}},
         {"type": "text", "text": "world"}]
    assert _text_from_content(c) == "hello world"
    out = anthropic_to_openai({"model": "claude", "messages": [{"role": "user", "content": c}]})
    assert out["messages"][0]["content"] == "hello world"


def test_stop_sequences_and_tools():
    out = anthropic_to_openai({"model": "claude", "messages": [], "stop_sequences": ["X"],
                               "tools": [{"name": "t"}], "tool_choice": {"type": "auto"}})
    assert out["stop"] == ["X"]
    assert "tools" not in out and "tool_choice" not in out


# ---- response: OpenAI -> Anthropic ----

def test_response_mapping():
    a = openai_to_anthropic({"id": "abc", "choices": [{"message": {"content": "hi there"},
                                                       "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, "claude")
    assert a["type"] == "message" and a["role"] == "assistant" and a["model"] == "claude"
    assert a["content"] == [{"type": "text", "text": "hi there"}]
    assert a["stop_reason"] == "end_turn"
    assert a["usage"] == {"input_tokens": 5, "output_tokens": 2}
    assert a["id"].startswith("msg_")


def test_finish_reason_length_maps_max_tokens():
    a = openai_to_anthropic({"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]},
                            "codex")
    assert a["stop_reason"] == "max_tokens"


def test_empty_content_safe():
    a = openai_to_anthropic({"choices": [{"message": {}, "finish_reason": "stop"}]}, "claude")
    assert a["content"][0]["text"] == ""


# ---- streaming events ----

def test_stream_event_sequence():
    frames = openai_sse_to_anthropic_events("claude", ["He", "llo"])
    text = b"".join(frames).decode()
    # ordered Anthropic event protocol
    order = [text.index(e) for e in ("message_start", "content_block_start",
                                     "content_block_delta", "content_block_stop",
                                     "message_delta", "message_stop")]
    assert order == sorted(order)
    assert text.count("event: content_block_delta") == 2   # one event frame per non-empty delta
    assert '"text_delta"' in text and '"text": "He"' in text and '"text": "llo"' in text
    assert text.startswith("event: message_start\ndata: ")


def test_stream_skips_empty_deltas():
    frames = openai_sse_to_anthropic_events("claude", ["", "hi", ""])
    assert b"".join(frames).decode().count("event: content_block_delta") == 1


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
