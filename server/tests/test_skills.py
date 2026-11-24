"""Skills: SKILL.md format, service layer, REST API."""

import io
import zipfile

import httpx
import pytest
from sqlalchemy import select

from concierge.auth.models import Role
from concierge.core import db
from concierge.skills import format as skill_format
from concierge.skills import service
from concierge.skills.models import Skill
from tests.conftest import auth, login, make_user

SKILL_MD = """---
name: shopping
description: Помогает составить список покупок
allowed-tools: [Read, Write]
metadata:
  author: test
aliases: [Покупки]
triggers:
  - список покупок
argument-hint: "<магазин>"
---

# Покупки
Спроси про магазин: $ARGUMENTS.
"""

SKILL_MD_NOSTORE = """---
name: notes
description: Заметки и напоминалки
---

# Заметки
Тело без аргументов.
"""


def _md(**over) -> str:
    meta = {
        "name": "shopping",
        "description": "Помогает составить список покупок",
        **over,
    }
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---\n\nТело.\n")  # noqa: RUF001
    return "\n".join(lines)


async def _role(name: str, perms: dict) -> None:
    async with db.get_sessionmaker()() as session:
        session.add(Role(name=name, permissions=perms))
        await session.commit()


async def _skill_role_user(
    username: str, perms: dict | None = None
) -> "object":
    """User on a role with the given permissions (default: create_skills)."""
    role_name = f"role_{username}"
    await _role(
        role_name,
        perms
        if perms is not None
        else {"create_skills": True, "tools": {"allow": ["*"], "deny": []}},
    )
    return await make_user(username, role_name)


# ---------- format ----------


async def test_format_parse_valid(engine) -> None:
    doc = skill_format.parse_skill_md(SKILL_MD)
    assert doc.name == "shopping"
    assert doc.description.startswith("Помогает")
    assert doc.aliases == ["покупки"]
    assert doc.triggers == ["список покупок"]
    assert doc.argument_hint == "<магазин>"
    assert doc.body.startswith("# Покупки")
    # Unknown keys (allowed-tools, metadata) stay in the stored text.
    assert "allowed-tools" in doc.content


async def test_format_ephemeral_flag(engine) -> None:
    assert skill_format.parse_skill_md(SKILL_MD).ephemeral is False
    assert skill_format.parse_skill_md(_md(ephemeral=True)).ephemeral is True
    with pytest.raises(skill_format.SkillFormatError):
        skill_format.parse_skill_md(_md(ephemeral='"yes"'))


async def test_format_rejections(engine) -> None:
    for bad in (
        _md(name=""),
        _md(description=""),
        _md(name="My Skill"),
        _md(name="stop"),
        _md(triggers=["ок"]),
        "---\nname: [unclosed\n---\nтело",  # noqa: RUF001
        "без заголовка вообще",
    ):
        with pytest.raises(skill_format.SkillFormatError):
            skill_format.parse_skill_md(bad)


async def test_format_render_body(engine) -> None:
    body = "Магазин: $ARGUMENTS; первый: $1; второй: $ARGUMENTS[1]; dir ${CLAUDE_SKILL_DIR}/f.txt"
    out = skill_format.render_body(body, "ашан 'пятёрочка'", "shopping")
    assert "ашан 'пятёрочка'" in out
    assert "первый: ашан" in out
    assert "второй: пятёрочка" in out
    assert "skill:shopping/f.txt" in out

    # No $ARGUMENTS marker -> args appended.
    out = skill_format.render_body("Тело", "что-то", "s")
    assert out.endswith("ARGUMENTS: что-то")


async def test_format_match_trigger(engine) -> None:
    phrases = ["составь список покупок", "список покупок"]
    assert skill_format.match_trigger(phrases, "Составь список покупок, пожалуйста")
    assert skill_format.match_trigger(phrases, "нужен СПИСОК ПОКУПОК!")
    assert skill_format.match_trigger(phrases, "ёлки: список покупок")
    assert not skill_format.match_trigger(phrases, "списки покупателя")
    assert not skill_format.match_trigger(phrases, "покупок нет")
    # ё normalized
    assert skill_format.match_trigger(["напомни о встрече"], "напомни о встрече завтра")  # noqa: RUF001


async def test_format_zip_roundtrip(engine) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("shopping/SKILL.md", SKILL_MD)
        zf.writestr("shopping/references/units.md", "единицы: шт, кг")
        zf.writestr("shopping/bin/blob.bin", b"\xff\x00\xfe")
        zf.writestr("other/readme.txt", "чужая папка")
    content, files, skipped = skill_format.unpack_archive(
        buf.getvalue(), "x.zip"
    )
    assert content == SKILL_MD
    assert files == {"references/units.md": "единицы: шт, кг"}
    assert skipped == ["shopping/bin/blob.bin"]

    out = skill_format.pack_archive("shopping", SKILL_MD, files)
    content2, files2, skipped2 = skill_format.unpack_archive(out, "y.zip")
    assert content2 == SKILL_MD and files2 == files and skipped2 == []


# ---------- service ----------


async def _create(session, owner_id, content, **kw) -> Skill:
    return await service.create_skill(
        session, owner_id, content, created_by=1, **kw
    )


async def test_service_visibility_and_shadowing(engine) -> None:
    admin = await make_user("skadm", "admin")
    kid = await make_user("skkid", "child")
    adult = await make_user("skad", "adult")
    async with db.get_sessionmaker()() as session:
        child_role = kid.role_id
        await _create(session, None, SKILL_MD)
        # role-restricted shared skill visible only to `child`
        await _create(
            session,
            None,
            _md(name="kidonly", description="только детям"),
            role_ids=[child_role],
        )
        # personal shadow of `shopping` for admin
        await _create(
            session,
            admin.id,
            _md(description="личный вариант"),
        )
        names_admin = {
            s.name: s for s in await service.available_skills(session, admin)
        }
        names_kid = {
            s.name for s in await service.available_skills(session, kid)
        }
        names_adult = {
            s.name for s in await service.available_skills(session, adult)
        }
    assert names_admin["shopping"].owner_id == admin.id  # personal shadows
    assert names_admin["shopping"].description == "личный вариант"
    assert "kidonly" in names_kid and "kidonly" not in names_adult
    assert "shopping" in names_adult and "shopping" in names_kid


async def test_service_ephemeral_denorm(engine) -> None:
    user = await make_user("skeph", "adult")
    async with db.get_sessionmaker()() as session:
        skill = await _create(session, user.id, _md(ephemeral=True))
        assert skill.ephemeral is True
        skill = await service.update_skill(session, skill, content=SKILL_MD)
        assert skill.ephemeral is False


async def test_service_resolve_and_collisions(engine) -> None:
    user = await make_user("skres", "adult")
    async with db.get_sessionmaker()() as session:
        await _create(session, user.id, SKILL_MD)
        assert (
            await service.resolve(session, user, "SHOPPING")
        ) is not None
        assert (await service.resolve(session, user, "покупки")) is not None
        assert (await service.resolve(session, user, "nope")) is None
        # A different name in the same scope is fine...
        await _create(session, user.id, SKILL_MD_NOSTORE)
        # ...but a duplicate name is a collision.
        with pytest.raises(service.SkillConflictError):
            await _create(session, user.id, SKILL_MD)
        # alias colliding with existing name
        with pytest.raises(service.SkillConflictError):
            await _create(
                session,
                user.id,
                _md(name="other", aliases=["shopping"], description="x"),
            )


async def test_service_update_and_files(engine) -> None:
    user = await make_user("skupd", "adult")
    async with db.get_sessionmaker()() as session:
        skill = await _create(session, user.id, SKILL_MD)
        assert skill.version == 1
        await service.update_skill(
            session, skill, content=_md(description="новое описание")
        )
        assert skill.version == 2
        await service.update_skill(session, skill, enabled=False)
        assert skill.enabled is False and skill.version == 2

        await service.put_file(session, skill, "references/a.md", "текст")
        with pytest.raises(skill_format.SkillFormatError):
            await service.put_file(session, skill, "../evil.md", "x")
        with pytest.raises(skill_format.SkillFormatError):
            await service.put_file(session, skill, "SKILL.md", "x")
        row = await service.get_file(session, skill, "references/a.md")
        assert row is not None and row.content == "текст"


async def test_service_activate_deactivate(engine) -> None:
    from concierge.conversations import service as conv_service
    from concierge.conversations.models import Conversation

    user = await make_user("skact", "adult")
    async with db.get_sessionmaker()() as session:
        skill = await _create(session, user.id, SKILL_MD)
        conv = await conv_service.create_conversation(
            session, user, "personal", "", []
        )
        assert conv is not None
        msg = await service.activate(session, conv, skill, "ашан", user)
        assert msg.parts[0]["type"] == "skill_event"
        assert msg.parts[0]["action"] == "start"
        state = conv.settings["active_skill"]
        assert state["skill_id"] == skill.id and state["args"] == "ашан"

        active = await service.active_skill_state(session, conv)
        assert active is not None and active[0].id == skill.id

        msg = await service.deactivate(session, conv, user, "stop")
        assert msg is not None and msg.parts[0]["action"] == "end"
        conv2 = await session.get(Conversation, conv.id)
        assert conv2 is not None
        assert "active_skill" not in (conv2.settings or {})

        # Stale pointer (deleted skill) is cleared silently.
        skill2 = await _create(session, user.id, SKILL_MD_NOSTORE)
        await service.activate(session, conv, skill2, "", user)
        await service.delete_skill(session, skill2)
        assert await service.active_skill_state(session, conv) is None
        assert "active_skill" not in (conv.settings or {})


# ---------- api ----------


async def test_api_permissions(engine, client: httpx.AsyncClient) -> None:
    await make_user("noperm", "adult")
    token = await login(client, "noperm")
    resp = await client.post(
        "/skills/mine", json={"content": SKILL_MD}, headers=auth(token)
    )
    assert resp.status_code == 403
    resp = await client.get("/admin/skills", headers=auth(token))
    assert resp.status_code == 403

    await make_user("adm", "admin")
    token = await login(client, "adm")
    resp = await client.post(
        "/admin/skills", json={"content": SKILL_MD}, headers=auth(token)
    )
    assert resp.status_code == 201, resp.text


async def test_api_personal_crud_and_422(engine, client: httpx.AsyncClient) -> None:
    await _skill_role_user("mine1")
    token = await login(client, "mine1")
    resp = await client.post(
        "/skills/mine", json={"content": "не формат"}, headers=auth(token)
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/skills/mine", json={"content": SKILL_MD}, headers=auth(token)
    )
    assert resp.status_code == 201, resp.text
    skill_id = resp.json()["id"]

    resp = await client.post(
        "/skills/mine", json={"content": SKILL_MD}, headers=auth(token)
    )
    assert resp.status_code == 409  # same name in the same scope

    # Another user's personal skill is invisible.
    await _skill_role_user("mine2")
    token2 = await login(client, "mine2")
    assert (
        await client.get(f"/skills/mine/{skill_id}", headers=auth(token2))
    ).status_code == 404
    names = (
        await client.get("/skills", headers=auth(token2))
    ).json()
    assert all(s["personal"] is False or s["name"] != "shopping" for s in names)

    # Owner sees it via /skills as personal.
    names = (await client.get("/skills", headers=auth(token))).json()
    own = [s for s in names if s["name"] == "shopping"]
    assert own and own[0]["personal"] is True

    # Files round-trip.
    resp = await client.put(
        f"/skills/mine/{skill_id}/files/references/units.md",
        json={"content": "единицы"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    resp = await client.get(
        f"/skills/mine/{skill_id}", headers=auth(token)
    )
    assert resp.json()["files"] == ["references/units.md"]
    resp = await client.delete(
        f"/skills/mine/{skill_id}/files/references/units.md",
        headers=auth(token),
    )
    assert resp.status_code == 200

    # Export/import round-trip.
    resp = await client.get(
        f"/skills/mine/{skill_id}/export", headers=auth(token)
    )
    assert resp.status_code == 200
    resp = await client.post(
        "/skills/mine/import",
        files={"file": ("shopping.zip", resp.content, "application/zip")},
        headers=auth(token2),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["skill"]["name"] == "shopping"


async def test_api_shared_role_visibility(
    engine, client: httpx.AsyncClient
) -> None:
    await make_user("skadm2", "admin")
    token = await login(client, "skadm2")
    async with db.get_sessionmaker()() as session:
        child_role = (
            await session.execute(select(Role).where(Role.name == "child"))
        ).scalar_one()
    resp = await client.post(
        "/admin/skills",
        json={"content": _md(name="kidviz", description="для детей"),
              },
        headers=auth(token),
    )
    assert resp.status_code == 201
    skill_id = resp.json()["id"]
    resp = await client.patch(
        f"/admin/skills/{skill_id}",
        json={"role_ids": [child_role.id]},
        headers=auth(token),
    )
    assert resp.status_code == 200

    await make_user("kid2", "child")
    await make_user("ad2", "adult")
    t_kid = await login(client, "kid2")
    t_ad = await login(client, "ad2")
    names_kid = {s["name"] for s in (await client.get("/skills", headers=auth(t_kid))).json()}
    names_ad = {s["name"] for s in (await client.get("/skills", headers=auth(t_ad))).json()}
    assert "kidviz" in names_kid and "kidviz" not in names_ad


async def test_api_clear_active_skill(
    engine, client: httpx.AsyncClient
) -> None:
    user = await _skill_role_user("clearer")
    token = await login(client, "clearer")
    resp = await client.post(
        "/conversations",
        json={"kind": "personal", "title": "", "member_ids": []},
        headers=auth(token),
    )
    cid = resp.json()["id"]
    async with db.get_sessionmaker()() as session:
        from concierge.conversations.models import Conversation

        skill = await service.create_skill(
            session, user.id, SKILL_MD, created_by=user.id
        )
        conv = await session.get(Conversation, cid)
        assert conv is not None
        await service.activate(session, conv, skill, "", user)
    conv_out = (await client.get(f"/conversations/{cid}", headers=auth(token))).json()
    assert conv_out["active_skill"]["name"] == "shopping"

    resp = await client.delete(
        f"/conversations/{cid}/skill", headers=auth(token)
    )
    assert resp.status_code == 200
    conv_out = (await client.get(f"/conversations/{cid}", headers=auth(token))).json()
    assert conv_out["active_skill"] is None
