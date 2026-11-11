"""Session-log serialization: thinking/tool calls/tool returns persist in
Message.parts and are replayed as real pydantic-ai messages, plus the
compaction marker handling in _build_history."""

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from concierge.agent import history as agent_history
from concierge.agent.runtime import get_runtime
from concierge.conversations.models import Conversation, Message


def test_serialize_deserialize_roundtrip() -> None:
    msgs = [
        ModelRequest(parts=[UserPromptPart("привет")]),
        ModelResponse(
            parts=[
                ThinkingPart(
                    "надо поискать",
                    signature="sig-1",
                    provider_name="anthropic",
                ),
                ToolCallPart(
                    tool_name="search_messages",
                    args={"q": "встреча"},
                    tool_call_id="c1",
                ),
                ToolCallPart(
                    tool_name="ask_model",
                    args={"model": "Алиса", "question": "жива?"},
                    tool_call_id="c2",
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search_messages",
                    content={"found": 2},
                    tool_call_id="c1",
                ),
                RetryPromptPart(
                    content="boom",
                    tool_name="ask_model",
                    tool_call_id="c2",
                ),
            ]
        ),
        ModelResponse(parts=[TextPart("готово")]),
    ]
    parts = agent_history.serialize_parts(msgs)

    types = [p["type"] for p in parts]
    assert types == [
        "thinking",
        "tool_call",
        "tool_call",
        "tool_return",
        "tool_return",
        "text",
    ]
    assert parts[0]["signature"] == "sig-1"
    assert parts[1]["args"] == {"q": "встреча"}
    assert parts[2]["args"]["question"] == "жива?"
    assert parts[3]["content"] == {"found": 2}
    assert parts[4]["retry"] is True

    rebuilt = agent_history.deserialize_parts(parts)
    assert len(rebuilt) == 3

    resp1 = rebuilt[0]
    assert isinstance(resp1, ModelResponse)
    assert isinstance(resp1.parts[0], ThinkingPart)
    assert resp1.parts[0].content == "надо поискать"
    assert resp1.parts[0].signature == "sig-1"
    assert resp1.parts[0].provider_name == "anthropic"
    assert isinstance(resp1.parts[1], ToolCallPart)
    assert resp1.parts[1].args == {"q": "встреча"}
    assert resp1.parts[1].tool_call_id == "c1"

    req = rebuilt[1]
    assert isinstance(req, ModelRequest)
    assert isinstance(req.parts[0], ToolReturnPart)
    assert req.parts[0].content == {"found": 2}
    assert isinstance(req.parts[1], RetryPromptPart)
    assert req.parts[1].tool_call_id == "c2"

    resp2 = rebuilt[2]
    assert isinstance(resp2, ModelResponse)
    assert resp2.parts[0].content == "готово"


def test_deserialize_synthesizes_interrupted_calls() -> None:
    parts = [
        {
            "type": "tool_call",
            "name": "remember",
            "tool_call_id": "c1",
            "args": {"k": "v"},
        },
        {"type": "text", "text": "упал на полуслове"},
    ]
    rebuilt = agent_history.deserialize_parts(parts)
    assert len(rebuilt) == 2
    resp, req = rebuilt
    assert isinstance(resp, ModelResponse)
    assert isinstance(resp.parts[0], ToolCallPart)
    assert isinstance(req, ModelRequest)
    assert isinstance(req.parts[0], ToolReturnPart)
    assert req.parts[0].tool_call_id == "c1"
    assert req.parts[0].outcome == "failed"


def test_deserialize_drops_orphan_return_and_legacy_tool() -> None:
    parts = [
        {
            "type": "tool_return",
            "name": "ghost",
            "tool_call_id": "nope",
            "content": "x",
        },
        {"type": "tool", "name": "legacy", "tool_call_id": "l1", "status": "done"},
        {"type": "text", "text": "ok"},
    ]
    rebuilt = agent_history.deserialize_parts(parts)
    assert len(rebuilt) == 1
    assert isinstance(rebuilt[0], ModelResponse)
    assert rebuilt[0].parts == [TextPart("ok")]


def test_multi_call_batch_preserves_real_returns() -> None:
    parts = [
        {"type": "tool_call", "name": "a", "tool_call_id": "c1", "args": {}},
        {"type": "tool_call", "name": "b", "tool_call_id": "c2", "args": {}},
        {"type": "tool_return", "name": "a", "tool_call_id": "c1", "content": "r1"},
        {"type": "tool_return", "name": "b", "tool_call_id": "c2", "content": "r2"},
        {"type": "text", "text": "done"},
    ]
    rebuilt = agent_history.deserialize_parts(parts)
    assert len(rebuilt) == 3
    req = rebuilt[1]
    assert isinstance(req, ModelRequest)
    assert [p.content for p in req.parts] == ["r1", "r2"]


def _msg(parts: list[dict], kind: str, seq: int) -> Message:
    return Message(
        id=seq,
        conversation_id=1,
        seq=seq,
        author_kind=kind,
        author_id=1 if kind == "user" else None,
        parts=parts,
    )


def test_build_history_compaction_marker() -> None:
    conv = Conversation(id=1, kind="personal", owner_id=1)
    rows = [
        _msg([{"type": "text", "text": "старое сообщение"}], "user", 1),
        _msg([{"type": "text", "text": "старый ответ"}], "assistant", 2),
        _msg(
            [
                {
                    "type": "compaction",
                    "text": "конспект: обсуждали X",
                    "upto_seq": 2,
                    "count": 2,
                }
            ],
            "system",
            3,
        ),
        _msg([{"type": "text", "text": "новый вопрос"}], "user", 4),
        _msg(
            [
                {
                    "type": "tool_call",
                    "name": "recall",
                    "tool_call_id": "t1",
                    "args": {"q": "x"},
                },
                {
                    "type": "tool_return",
                    "name": "recall",
                    "tool_call_id": "t1",
                    "content": "ничего",
                },
                {"type": "text", "text": "ответ"},
            ],
            "assistant",
            5,
        ),
    ]
    out = get_runtime()._build_history(rows, conv, {}, {})
    # marker drops rows 1-2 and becomes the summary prompt
    assert len(out) == 5
    assert isinstance(out[0], ModelRequest)
    assert isinstance(out[0].parts[0], UserPromptPart)
    assert "конспект: обсуждали X" in str(out[0].parts[0].content)
    assert isinstance(out[1], ModelRequest)  # "новый вопрос"
    assert isinstance(out[2], ModelResponse)  # tool_call
    assert isinstance(out[3], ModelRequest)  # tool_return
    # the final text part is a separate response after the returns
    assert isinstance(out[3].parts[0], ToolReturnPart)
    assert isinstance(out[4], ModelResponse)
    assert out[4].parts[0].content == "ответ"


def test_estimate_parts_tokens() -> None:
    parts = [{"type": "text", "text": "x" * 400}]
    assert agent_history.estimate_parts_tokens(parts) == 100


def test_render_for_summary() -> None:
    parts = [
        {"type": "text", "text": "вопрос"},
        {"type": "thinking", "content": "скрыто"},
        {"type": "tool_call", "name": "recall", "tool_call_id": "c", "args": {"q": 1}},
        {"type": "tool_return", "name": "recall", "tool_call_id": "c", "content": "r"},
    ]
    text = agent_history.render_for_summary(parts)
    assert "вопрос" in text
    assert "скрыто" not in text
    assert "recall" in text
    assert "r" in text
