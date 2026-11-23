import argparse
import asyncio
import getpass

from sqlalchemy import select

from concierge.auth import security
from concierge.auth.models import Role, User
from concierge.core.db import get_sessionmaker
from concierge.media import service as media_service
from concierge.media import speaker
from concierge.media.models import Attachment


async def _bootstrap_admin(
    username: str, display_name: str, password: str
) -> int:
    async with get_sessionmaker()() as session:
        role = (
            await session.execute(select(Role).where(Role.name == "admin"))
        ).scalar_one_or_none()
        if role is None:
            print("error: 'admin' role not found — run `alembic upgrade head` first")
            return 1
        exists = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if exists is not None:
            print(f"error: user '{username}' already exists")
            return 1
        session.add(
            User(
                username=username,
                display_name=display_name,
                role_id=role.id,
                password_hash=security.hash_password(password),
            )
        )
        await session.commit()
    print(f"admin user '{username}' created")
    return 0


async def _backfill_speaker(limit: int | None, conversation_id: int | None) -> int:
    """Embed existing audio attachments and assign them to voices.

    Idempotent: attachments that already carry derived.speaker (or a
    voice_samples row — UNIQUE per attachment) are skipped/upserted.
    """
    async with get_sessionmaker()() as session:
        stmt = select(Attachment).order_by(Attachment.id)
        if conversation_id is not None:
            stmt = stmt.where(Attachment.conversation_id == conversation_id)
        ids = [
            a.id
            for a in (await session.execute(stmt)).scalars()
            if media_service.kind_of(a.mime, a.original_name) == "audio"
            and not (a.derived or {}).get("speaker")
        ]
    processed = errors = 0
    for att_id in ids[: limit or None]:
        async with get_sessionmaker()() as session:
            att = await session.get(Attachment, att_id)
            if att is None or (att.derived or {}).get("speaker"):
                continue
            user = await session.get(User, att.uploaded_by)
            if user is None or not speaker.speaker_id_allowed(user, att):
                continue
            vec, params = await speaker.embed_attachment(session, att)
            if vec is None:
                errors += 1
                continue
            duration = att.derived.get("duration_s")
            min_dur = float(
                params.get("min_duration_s", speaker.MIN_DURATION_S)
            )
            if duration is not None and duration < min_dur:
                continue
            sp = await speaker.assign_voice(session, att, vec, params)
            att.derived = {**(att.derived or {}), "speaker": sp}
            await session.commit()
            processed += 1
            print(f"  attachment {att_id}: {sp['name']} (score {sp['score']})")
    print(f"backfill-speaker: {processed} processed, {errors} failed")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="concierge")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap-admin", help="create the first admin user")
    boot.add_argument("--username", default="admin")
    boot.add_argument("--display-name", default="Admin")
    boot.add_argument("--password", default=None)
    fill = sub.add_parser(
        "backfill-speaker", help="assign voices to existing audio attachments"
    )
    fill.add_argument("--limit", type=int, default=None)
    fill.add_argument("--conversation-id", type=int, default=None)
    args = parser.parse_args()

    if args.command == "bootstrap-admin":
        password = args.password or getpass.getpass("Password: ")
        if len(password) < 8:
            print("error: password must be at least 8 characters")
            raise SystemExit(1)
        raise SystemExit(
            asyncio.run(
                _bootstrap_admin(args.username, args.display_name, password)
            )
        )

    if args.command == "backfill-speaker":
        raise SystemExit(
            asyncio.run(_backfill_speaker(args.limit, args.conversation_id))
        )


if __name__ == "__main__":
    main()
