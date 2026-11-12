"""Attachment storage and helpers.

Blobs are content-addressed under `settings.blob_dir` (`<dir>/<sha2>/<sha256>`)
so identical files are stored once; the `attachments` row holds per-upload
metadata and the pipeline writes its results into `derived`.
"""

import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.conversations.models import Conversation, ConversationMember
from concierge.core.config import get_settings
from concierge.core.db import get_sessionmaker
from concierge.media.models import Attachment

log = logging.getLogger(__name__)

_KIND_BY_MIME_PREFIX = {"image": "image", "audio": "audio", "video": "video"}
_DOCUMENT_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/json",
}
_EXT_KIND = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".heic": "image",
    ".ogg": "audio",
    ".opus": "audio",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".flac": "audio",
    ".webm": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".avi": "video",
    ".pdf": "document",
    ".docx": "document",
    ".doc": "document",
    ".xlsx": "document",
    ".xls": "document",
    ".pptx": "document",
    ".ppt": "document",
    ".txt": "document",
    ".md": "document",
    ".csv": "document",
    ".json": "document",
}


def blob_path(sha256: str) -> Path:
    return Path(get_settings().blob_dir) / sha256[:2] / sha256


def write_blob(data: bytes) -> str:
    """Store `data` under its sha256 (dedup); returns the hash."""
    sha = hashlib.sha256(data).hexdigest()
    path = blob_path(sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return sha


def read_blob(sha256: str) -> bytes:
    return blob_path(sha256).read_bytes()


def kind_of(mime: str, name: str) -> str:
    """image | audio | video | document | other — drives the MediaPipeline."""
    prefix = mime.split("/", 1)[0].lower()
    if mime.lower() in _DOCUMENT_MIMES:
        return "document"
    if mime.lower().startswith("text/"):
        return "document"
    if prefix in _KIND_BY_MIME_PREFIX:
        return _KIND_BY_MIME_PREFIX[prefix]
    return _EXT_KIND.get(Path(name).suffix.lower(), "other")


def max_upload_bytes(user: User) -> int:
    return int(user.role.permissions.get("max_upload_mb") or 0) * 1024 * 1024


async def create_attachment(
    session: AsyncSession,
    user: User,
    name: str,
    mime: str,
    data: bytes,
    conversation_id: int | None,
) -> tuple[Attachment, bool]:
    """Insert an attachment row; (sha256, uploader, conversation) dedups retries."""
    sha = write_blob(data)
    existing = (
        await session.execute(
            select(Attachment).where(
                Attachment.sha256 == sha,
                Attachment.uploaded_by == user.id,
                Attachment.conversation_id.is_(None)
                if conversation_id is None
                else Attachment.conversation_id == conversation_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing, False
    att = Attachment(
        sha256=sha,
        mime=mime or "application/octet-stream",
        size=len(data),
        original_name=name[:512],
        uploaded_by=user.id,
        conversation_id=conversation_id,
        derived={"status": "pending"},
    )
    session.add(att)
    await session.commit()
    await session.refresh(att)
    return att, True


async def bind_to_conversation(
    session: AsyncSession, att: Attachment, conversation_id: int
) -> None:
    if att.conversation_id != conversation_id:
        att.conversation_id = conversation_id
        await session.commit()


async def can_access(session: AsyncSession, att: Attachment, user: User) -> bool:
    if att.uploaded_by == user.id or has_permission(user.role.permissions, "*"):
        return True
    if att.conversation_id is None:
        return False
    conv = await session.get(Conversation, att.conversation_id)
    if conv is None or conv.status == "deleted":
        return False
    member = await session.get(
        ConversationMember, (att.conversation_id, user.id)
    )
    return member is not None


def attachment_to_dict(att: Attachment) -> dict[str, Any]:
    """API/WS payload for an attachment."""
    out: dict[str, Any] = {
        "id": att.id,
        "sha256": att.sha256,
        "mime": att.mime,
        "size": att.size,
        "name": att.original_name,
        "kind": kind_of(att.mime, att.original_name),
        "conversation_id": att.conversation_id,
        "uploaded_by": att.uploaded_by,
        "status": att.derived.get("status", "pending"),
        "created_at": att.created_at.isoformat() if att.created_at else None,
    }
    for key in ("transcript", "caption", "text", "duration_s", "pages", "error"):
        if key in att.derived:
            out[key] = att.derived[key]
    return out


def derived_text(att: Attachment) -> str:
    """The readable content the pipeline produced for this attachment."""
    for key in ("transcript", "caption", "text"):
        value = att.derived.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def media_prompt_text(att: Attachment, part_type: str) -> str:
    """How an attachment reads to the agent inside a message."""
    transcript = str(att.derived.get("transcript") or "").strip()
    caption = str(att.derived.get("caption") or "").strip()
    text = str(att.derived.get("text") or "").strip()
    if part_type == "audio":
        return transcript or caption
    if part_type == "video":
        return "\n".join(x for x in (transcript, caption) if x)
    return text or caption


def expand_part(part: dict[str, Any], att: Attachment | None) -> dict[str, Any]:
    """Attach derived fields to a message part for API/WS payloads."""
    if att is None:
        return {**part, "status": "missing"}
    out = {**part, "name": att.original_name, "mime": att.mime}
    out["status"] = att.derived.get("status", "pending")
    for key in ("transcript", "caption", "text", "duration_s", "pages", "error"):
        if key in att.derived:
            out[key] = att.derived[key]
    return out


def part_attachment_id(part: dict[str, Any]) -> int | None:
    raw = part.get("attachment_id")
    return raw if isinstance(raw, int) else None


async def expand_messages(
    session: AsyncSession, messages: list[Any]
) -> dict[int, list[dict[str, Any]]]:
    """message_id -> expanded parts, batching the attachment lookup."""
    ids = {
        aid
        for m in messages
        for p in m.parts
        if (aid := part_attachment_id(p)) is not None
    }
    atts: dict[int, Attachment] = {}
    if ids:
        rows = (
            await session.execute(
                select(Attachment).where(Attachment.id.in_(ids))
            )
        ).scalars()
        atts = {a.id: a for a in rows}
    return {
        m.id: [
            expand_part(p, atts.get(aid))
            if (aid := part_attachment_id(p)) is not None
            else p
            for p in m.parts
        ]
        for m in messages
    }


async def transcribe(attachment_id: int) -> str:
    """Public for plans 7/8: run the pipeline if needed, return the transcript."""
    from concierge.media.pipeline import get_pipeline

    await get_pipeline().ensure_processed(attachment_id)
    async with get_sessionmaker()() as session:
        att = await session.get(Attachment, attachment_id)
        if att is None:
            return ""
        return str(att.derived.get("transcript") or "")
