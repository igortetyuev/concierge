"""Serialize a run's pydantic-ai messages into persisted `Message.parts` and
rebuild `message_history` back for the next run.

The session log keeps the agent loop faithful across turns, like other agents
(Devin, pi, hermes) do: tool calls keep their args, tool returns keep their
content, and thinking blocks keep the provider signature/`provider_details`
needed to round-trip them (required e.g. by Anthropic).

Stored part shapes (all plain JSON dicts):
- {"type": "thinking", "content", "id"?, "signature"?, "provider_name"?,
   "provider_details"?}
- {"type": "tool_call", "name", "tool_call_id", "args", "native"?,
   "tool_kind"?, "id"?, "provider_name"?, "provider_details"?}
- {"type": "tool_return", "name", "tool_call_id", "content", "native"?,
   "retry"?, "tool_kind"?, "metadata"?, "outcome"?, "provider_name"?,
   "provider_details"?}
- {"type": "text", "text"}
- {"type": "compaction", "text", "upto_seq", "count"} — system marker that
  replaces earlier history (context compression)
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    NativeToolCallPart,
    NativeToolReturnPart,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_core import to_jsonable_python

# Provider round-trip fields copied verbatim into storage.
_PROVIDER_FIELDS = ("id", "provider_name", "provider_details")

# Rough chars-per-token estimate for the compaction trigger (no tokenizer).
CHARS_PER_TOKEN = 4

TOOL_RESULT_SNIPPET = 300
TOOL_ARGS_SNIPPET = 200


def serialize_parts(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    """Flatten a run's new messages into stored `parts` dicts.

    User/system prompt parts are skipped: the trigger message is already a
    row of its own.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                out.extend(_dump_response_part(part))
        else:
            for req_part in msg.parts:
                dumped = _dump_request_part(req_part)
                if dumped is not None:
                    out.append(dumped)
    return out


def _provider_fields(part: Any, out: dict[str, Any]) -> dict[str, Any]:
    for name in _PROVIDER_FIELDS:
        value = getattr(part, name, None)
        if value is not None:
            out[name] = to_jsonable_python(value)
    return out


def _dump_response_part(part: ModelResponsePart) -> list[dict[str, Any]]:
    if isinstance(part, ThinkingPart):
        out: dict[str, Any] = {"type": "thinking", "content": part.content}
        if part.signature is not None:
            out["signature"] = to_jsonable_python(part.signature)
        return [_provider_fields(part, out)]
    if isinstance(part, TextPart):
        return [{"type": "text", "text": part.content}]
    if isinstance(part, NativeToolCallPart):
        return [_dump_tool_call(part, native=True)]
    if isinstance(part, NativeToolReturnPart):
        # Provider-side tool results (e.g. web_search) live inside responses.
        return [_dump_tool_return(part, native=True)]
    if isinstance(part, ToolCallPart):
        return [_dump_tool_call(part, native=False)]
    return []


def _dump_tool_call(
    part: ToolCallPart | NativeToolCallPart, *, native: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "tool_call",
        "name": part.tool_name,
        "tool_call_id": part.tool_call_id,
        "args": to_jsonable_python(part.args),
    }
    if part.tool_kind is not None:
        out["tool_kind"] = part.tool_kind
    if native:
        out["native"] = True
    return _provider_fields(part, out)


def _dump_request_part(part: ModelRequestPart) -> dict[str, Any] | None:
    if isinstance(part, ToolReturnPart):
        return _dump_tool_return(part, native=False)
    if isinstance(part, RetryPromptPart):
        return {
            "type": "tool_return",
            "name": part.tool_name,
            "tool_call_id": part.tool_call_id,
            "content": to_jsonable_python(part.content),
            "retry": True,
        }
    # UserPromptPart / SystemPromptPart — owned by the trigger row / instructions.
    return None


def _dump_tool_return(
    part: ToolReturnPart | NativeToolReturnPart, *, native: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "tool_return",
        "name": part.tool_name,
        "tool_call_id": part.tool_call_id,
        "content": to_jsonable_python(part.content),
    }
    if part.tool_kind is not None:
        out["tool_kind"] = part.tool_kind
    if part.metadata is not None:
        out["metadata"] = to_jsonable_python(part.metadata)
    if part.outcome != "success":
        out["outcome"] = part.outcome
    if native:
        out["native"] = True
    return _provider_fields(part, out)


def deserialize_parts(parts: list[dict[str, Any]]) -> list[ModelMessage]:
    """Rebuild the request/response sequence from stored `parts`.

    Response parts (thinking/tool_call/text) and request parts (tool_return)
    alternate; the stored order is replayed, flushing the pending message on
    each switch. Unknown/legacy part types are skipped.
    """
    out: list[ModelMessage] = []
    resp: list[ModelResponsePart] = []
    req: list[ModelRequestPart] = []
    # tool_call_ids awaiting a return. Providers reject dangling calls and
    # orphan returns, so both directions are reconciled here: a run that died
    # mid-call gets a synthetic "interrupted" return, and returns without a
    # matching call are dropped.
    open_calls: dict[str, str] = {}

    def flush_resp() -> None:
        nonlocal resp
        if resp:
            out.append(ModelResponse(parts=resp))
            resp = []

    def close_req() -> None:
        """Flush pending returns; synthesize "interrupted" returns for calls
        never answered. Calls are only dead once their return batch is over:
        a pending request exists, or the parts ended while a response was
        still being collected."""
        nonlocal req
        if req or (open_calls and not resp):
            for call_id, name in open_calls.items():
                req.append(
                    ToolReturnPart(
                        tool_name=name,
                        content="[вызов прерван]",
                        tool_call_id=call_id,
                        outcome="failed",
                    )
                )
            open_calls.clear()
        if req:
            out.append(ModelRequest(parts=req))
            req = []

    for p in parts:
        ptype = p.get("type")
        if ptype == "tool_return" and not p.get("native"):
            call_id = str(p.get("tool_call_id") or "")
            if call_id not in open_calls:
                continue
            request_part = _load_request_part(p)
            if request_part is not None:
                open_calls.pop(call_id)
                flush_resp()
                req.append(request_part)
            continue
        response_part = _load_response_part(p)
        if response_part is None and (
            ptype not in ("tool", "compaction")
            and p.get("attachment_id") is not None
        ):
            label = (
                "голосовой ответ"
                if p.get("tts")
                else p.get("name") or ptype or "attachment"
            )
            response_part = TextPart(f"[{label}]")
        if response_part is not None:
            close_req()
            if ptype == "tool_call" and not p.get("native"):
                open_calls[str(p.get("tool_call_id") or "")] = str(
                    p.get("name", "tool")
                )
            resp.append(response_part)
            continue
        # Legacy "tool" parts (name/status only) and anything unknown are
        # dropped: a dangling call would break provider validation.
    flush_resp()
    close_req()
    return out


def _load_response_part(p: dict[str, Any]) -> ModelResponsePart | None:
    ptype = p.get("type")
    extra: dict[str, Any] = {
        k: p[k] for k in _PROVIDER_FIELDS if k in p
    }
    if ptype == "thinking":
        return ThinkingPart(
            content=str(p.get("content", "")),
            signature=p.get("signature"),
            **extra,
        )
    if ptype == "text":
        return TextPart(str(p.get("text", "")))
    if ptype == "tool_call":
        cls = NativeToolCallPart if p.get("native") else ToolCallPart
        return cls(
            tool_name=str(p.get("name", "")),
            args=p.get("args"),
            tool_call_id=str(p.get("tool_call_id") or ""),
            tool_kind=p.get("tool_kind"),
            **extra,
        )
    if ptype == "tool_return" and p.get("native"):
        outcome = p.get("outcome")
        return NativeToolReturnPart(
            tool_name=str(p.get("name", "")),
            content=p.get("content"),
            tool_call_id=str(p.get("tool_call_id") or ""),
            tool_kind=p.get("tool_kind"),
            metadata=p.get("metadata"),
            outcome=outcome if outcome in ("success", "failed", "denied") else "success",
            **extra,
        )
    return None


def _load_request_part(p: dict[str, Any]) -> ModelRequestPart | None:
    extra: dict[str, Any] = {
        k: p[k] for k in _PROVIDER_FIELDS if k in p
    }
    if p.get("retry"):
        return RetryPromptPart(
            content=p.get("content") or "tool call failed",
            tool_name=p.get("name"),
            tool_call_id=str(p.get("tool_call_id") or ""),
        )
    outcome = p.get("outcome")
    return ToolReturnPart(
        tool_name=str(p.get("name", "")),
        content=p.get("content"),
        tool_call_id=str(p.get("tool_call_id") or ""),
        tool_kind=p.get("tool_kind"),
        metadata=p.get("metadata"),
        outcome=outcome if outcome in ("success", "failed", "denied") else "success",
        **extra,
    )


def compaction_part(
    summary: str, upto_seq: int, count: int, tokens_before: int
) -> dict[str, Any]:
    return {
        "type": "compaction",
        "text": summary,
        "upto_seq": upto_seq,
        "count": count,
        "tokens_before": tokens_before,
    }


def compaction_prompt(part: dict[str, Any]) -> str:
    """The marker as an injected history message (summary stands in for the
    dropped rows)."""
    count = part.get("count")
    header = "[Контекст сжат автоматически: предыдущий диалог заменён конспектом"
    if count:
        header += f" — {count} сообщ."
    return f"{header}]\n{part.get('text', '')}"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_parts_tokens(parts: list[dict[str, Any]]) -> int:
    total = 0
    for p in parts:
        ptype = p.get("type")
        if ptype == "text":
            total += len(str(p.get("text", "")))
        elif ptype == "thinking":
            total += len(str(p.get("content", "")))
        elif ptype == "tool_call":
            total += len(str(p.get("name", ""))) + len(
                str(to_jsonable_python(p.get("args")))
            )
        elif ptype == "tool_return":
            total += len(str(p.get("name", ""))) + len(
                str(to_jsonable_python(p.get("content")))
            )
        else:
            total += len(str(to_jsonable_python(p)))
    return max(1, total // CHARS_PER_TOKEN)


def render_for_summary(parts: list[dict[str, Any]]) -> str:
    """Render stored parts as text for the compaction summarizer: tool calls
    become one-line entries, thinking is dropped."""
    lines: list[str] = []
    for p in parts:
        ptype = p.get("type")
        if ptype == "text":
            lines.append(str(p.get("text", "")))
        elif ptype == "tool_call":
            args = str(to_jsonable_python(p.get("args")))[:TOOL_ARGS_SNIPPET]
            lines.append(f"[инструмент {p.get('name')}({args})]")
        elif ptype == "tool_return":
            content = str(to_jsonable_python(p.get("content")))
            mark = "ошибка: " if p.get("retry") or p.get("outcome") == "error" else ""
            lines.append(
                f"[{p.get('name')} → {mark}{content[:TOOL_RESULT_SNIPPET]}]"
            )
        elif ptype == "compaction":
            lines.append(f"[прежний конспект]\n{p.get('text', '')}")
        elif ptype == "choice":
            qs = "; ".join(
                str(q.get("question", "")) for q in p.get("questions") or []
            )
            lines.append(f"[вопрос пользователю] {qs}")
        elif ptype == "checklist":
            items = p.get("items") or []
            lines.append(
                f"[чек-лист] {p.get('title') or ''} ({len(items)} пунктов)"
            )
        elif ptype == "skill_event":
            verb = "запущен" if p.get("action") == "start" else "завершён"
            lines.append(f"[скилл «{p.get('name', '?')}» {verb}]")
        elif p.get("attachment_id") is not None:
            lines.append(f"[{p.get('name') or ptype or 'attachment'}]")
    return "\n".join(line for line in lines if line)
