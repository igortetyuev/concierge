import argparse
import asyncio
import getpass

from sqlalchemy import select

from concierge.auth import security
from concierge.auth.models import Role, User
from concierge.core.db import get_sessionmaker


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="concierge")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap-admin", help="create the first admin user")
    boot.add_argument("--username", default="admin")
    boot.add_argument("--display-name", default="Admin")
    boot.add_argument("--password", default=None)
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


if __name__ == "__main__":
    main()
