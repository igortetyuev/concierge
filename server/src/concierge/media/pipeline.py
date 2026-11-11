"""MediaPipeline: turns uploaded blobs into derived content in the background.

image → vision caption; audio → ffmpeg-normalized wav → stt transcript;
video → audio track → stt + frames → vision; document → text extraction.
Results land in `attachments.derived` and an `attachment.updated` event goes
to the conversation (or the uploader while the attachment is unbound).
"""

import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.channels import ChannelEvent, get_hub
from concierge.core.config import get_settings
from concierge.core.db import get_sessionmaker
from concierge.media import service, stt, vision
from concierge.media.documents import extract_document
from concierge.media.models import Attachment

log = logging.getLogger(__name__)

_VIDEO_FRAME_COUNT = 3
_VIDEO_FRAME_SCALE = "640:-2"


class MediaError(Exception):
    pass


class MediaPipeline:
    """One processing task per attachment; awaited by runs and transcribe()."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def schedule(self, attachment_id: int) -> None:
        if attachment_id in self._tasks:
            return
        task = asyncio.create_task(self._run(attachment_id))
        self._tasks[attachment_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(attachment_id, None))

    async def ensure_processed(
        self, attachment_id: int, timeout: float = 120.0
    ) -> None:
        """Await the pipeline for an attachment; runs it inline if the in-memory
        task is gone (restart) and the row is still unfinished."""
        task = self._tasks.get(attachment_id)
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout)
            return
        async with get_sessionmaker()() as session:
            att = await session.get(Attachment, attachment_id)
            status = (att.derived or {}).get("status") if att else None
        if att is None or status in ("done", "error", "skipped"):
            return
        await asyncio.wait_for(self._run(attachment_id), timeout)

    async def wait_for(self, attachment_ids: set[int], timeout: float = 120.0) -> None:
        for aid in attachment_ids:
            try:
                await self.ensure_processed(aid, timeout)
            except TimeoutError:
                log.warning("pipeline wait timed out attachment=%s", aid)

    async def _run(self, attachment_id: int) -> None:
        async with get_sessionmaker()() as session:
            att = await session.get(Attachment, attachment_id)
            if att is None:
                return
            if (att.derived or {}).get("status") in ("done", "error", "skipped"):
                return
            user = await session.get(User, att.uploaded_by)
            if user is None:
                att.derived = {**att.derived, "status": "error", "error": "uploader gone"}
                await session.commit()
                return
            att.derived = {**att.derived, "status": "processing"}
            await session.commit()
            try:
                extra = await self._extract(session, att, user)
                att.derived = {**att.derived, **extra, "status": "done"}
            except MediaSkippedError as skip:
                att.derived = {
                    **att.derived,
                    "status": "skipped",
                    "error": str(skip),
                }
            except Exception as exc:  # ffmpeg, provider, corrupt file
                log.exception("media pipeline failed attachment=%s", attachment_id)
                att.derived = {
                    **att.derived,
                    "status": "error",
                    "error": str(exc)[:500],
                }
            await session.commit()
        await self._publish(att)

    async def _extract(
        self, session: AsyncSession, att: Attachment, user: User
    ) -> dict[str, Any]:
        kind = service.kind_of(att.mime, att.original_name)
        if kind == "image":
            caption = await vision.describe_images(
                session, user, [(service.read_blob(att.sha256), att.mime)]
            )
            if caption is None:
                raise MediaSkippedError("no vision model bound")
            return {"caption": caption}
        if kind == "audio":
            data = service.read_blob(att.sha256)
            with tempfile.TemporaryDirectory() as tmp:
                src = _stage(data, tmp, att.original_name)
                wav = await _to_wav(src, Path(tmp) / "audio.wav")
                transcript = await stt.transcribe_audio(session, user, wav)
                return {
                    "transcript": transcript or "",
                    "duration_s": await _duration(src),
                }
        if kind == "video":
            data = service.read_blob(att.sha256)
            with tempfile.TemporaryDirectory() as tmp:
                src = _stage(data, tmp, att.original_name)
                out: dict[str, Any] = {"duration_s": await _duration(src)}
                track = await _try_audio_track(src, Path(tmp) / "audio.wav")
                if track is not None:
                    t = await stt.transcribe_audio(session, user, track)
                    if t is None:
                        raise MediaSkippedError("no stt provider bound")
                    out["transcript"] = t
                frames = await _video_frames(src, Path(tmp))
                if frames:
                    caption = await vision.describe_images(
                        session, user, frames, video=True
                    )
                    if caption:
                        out["caption"] = caption
                return out
        if kind == "document":
            return extract_document(
                att.mime, att.original_name, service.read_blob(att.sha256)
            )
        raise MediaSkippedError(f"unsupported media kind: {kind}")

    async def _publish(self, att: Attachment) -> None:
        event = ChannelEvent(
            "attachment.updated",
            att.conversation_id,
            {"attachment": service.attachment_to_dict(att)},
        )
        hub = get_hub()
        if att.conversation_id is not None:
            await hub.publish(event)
        else:
            await hub.publish_users({att.uploaded_by}, event)


class MediaSkippedError(Exception):
    """Capability unbound or media kind unsupported — not an error."""


def _stage(data: bytes, tmp: str, name: str) -> Path:
    suffix = Path(name).suffix or ".bin"
    src = Path(tmp) / f"src{suffix}"
    src.write_bytes(data)
    return src


async def _ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        get_settings().ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg: {err.decode('utf-8', 'replace')[:300]}")


async def _to_wav(src: Path, dst: Path) -> Path:
    await _ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dst))
    return dst


async def _try_audio_track(src: Path, dst: Path) -> Path | None:
    try:
        await _to_wav(src, dst)
    except MediaError:
        return None  # no audio track
    return dst


async def _video_frames(src: Path, tmp: Path) -> list[tuple[bytes, str]]:
    pattern = str(tmp / "frame%02d.jpg")
    await _ffmpeg(
        "-i",
        str(src),
        "-vf",
        f"fps=1/2,scale={_VIDEO_FRAME_SCALE}",
        "-frames:v",
        str(_VIDEO_FRAME_COUNT),
        pattern,
    )
    return [
        (p.read_bytes(), "image/jpeg")
        for p in sorted(tmp.glob("frame*.jpg"))
    ]


async def _duration(src: Path) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        get_settings().ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(src),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return round(float(out.strip()), 2)
    except ValueError:
        return None


_pipeline = MediaPipeline()


def get_pipeline() -> MediaPipeline:
    return _pipeline
