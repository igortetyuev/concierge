import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from concierge.auth.dependencies import CurrentUser, SessionDep
from concierge.conversations import service as conversations
from concierge.media import service
from concierge.media.pipeline import get_pipeline

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/attachments", status_code=201)
async def upload_attachment(
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    file: Annotated[UploadFile, File()],
    conversation_id: Annotated[int | None, Form()] = None,
) -> dict[str, Any]:
    """Upload a blob (multipart). Dedups on (sha256, uploader, conversation)."""
    max_bytes = service.max_upload_bytes(user)
    if max_bytes <= 0:
        raise HTTPException(403, "uploads are not allowed for this role")
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            413, f"file exceeds the {max_bytes // (1024 * 1024)} MB upload limit"
        )
    if not data:
        raise HTTPException(400, "empty file")
    if conversation_id is not None:
        conv = await conversations.get_member_conversation(
            session, conversation_id, user.id
        )
        if conv is None:
            raise HTTPException(404, "conversation not found")
    att, created = await service.create_attachment(
        session,
        user,
        file.filename or "file",
        file.content_type or "application/octet-stream",
        data,
        conversation_id,
    )
    if not created:
        response.status_code = 200
    get_pipeline().schedule(att.id)
    return service.attachment_to_dict(att)


@router.post("/attachments/{attachment_id}/retranscribe")
async def retranscribe_attachment(
    attachment_id: int, user: CurrentUser, session: SessionDep
) -> dict[str, Any]:
    """Reset derived state and run the pipeline again (STT retry)."""
    from concierge.media.models import Attachment

    att = await session.get(Attachment, attachment_id)
    if att is None or not await service.can_access(session, att, user):
        raise HTTPException(404, "attachment not found")
    if service.kind_of(att.mime, att.original_name) not in ("audio", "video"):
        raise HTTPException(400, "only audio/video can be transcribed")
    att.derived = {
        "status": "pending",
        **(
            {"origin": o}
            if (o := (att.derived or {}).get("origin"))
            else {}
        ),
    }
    await session.commit()
    get_pipeline().schedule(att.id)
    return service.attachment_to_dict(att)


@router.get("/attachments/{attachment_id}")
async def get_attachment(
    attachment_id: int, user: CurrentUser, session: SessionDep
) -> FileResponse:
    from concierge.media.models import Attachment

    att = await session.get(Attachment, attachment_id)
    if att is None or not await service.can_access(session, att, user):
        raise HTTPException(404, "attachment not found")
    return FileResponse(
        service.blob_path(att.sha256),
        media_type=att.mime,
        filename=att.original_name or None,
    )
