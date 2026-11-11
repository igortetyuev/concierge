import csv
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.settings import ModelSettings
from sqlalchemy import select

from concierge.agent import providers
from concierge.agent.providers import BoundModel
from concierge.agent.runtime import get_runtime
from concierge.agent.tools import ToolDeps, create_document
from concierge.channels import ChannelEvent, get_hub
from concierge.channels.telegram.sink import TelegramSink
from concierge.conversations import service as conversations
from concierge.conversations.models import Message, Run
from concierge.core import db
from concierge.documents.render import render_document
from concierge.media import service as media_service
from concierge.media.documents import extract_document
from concierge.media.models import Attachment
from tests.conftest import auth, login, make_user
from tests.test_media import _add_provider
from tests.test_telegram import FakeOutbox

MD = (
    "- молоко\n- хлеб\n\n"
    "## Продукты\n\n"
    "| Товар | Кол-во |\n"
    "| --- | --- |\n"
    "| Молоко | 2 |\n"
    "| Хлеб | 1 |\n"
)
TITLE = "Список покупок"


@pytest.mark.parametrize(
    "fmt", ["md", "txt", "csv", "docx", "xlsx", "pptx", "pdf"]
)
def test_render_and_extract(fmt: str) -> None:
    data, mime, name = render_document(fmt, TITLE, MD)
    assert name == f"{TITLE}.{fmt}"
    text = extract_document(mime, name, data)["text"]
    assert "Молоко" in text
    if fmt != "csv":
        assert TITLE in text


def test_render_xlsx_structure() -> None:
    import openpyxl

    data, _, _ = render_document("xlsx", TITLE, MD)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Продукты"]
    ws = wb["Продукты"]
    assert ws["B2"].value == 2 and isinstance(ws["B2"].value, int)
    assert ws["A1"].font.bold


def test_render_xlsx_numeric_cells() -> None:
    import openpyxl

    md = (
        "| Код | Значение |\n| --- | --- |\n"
        "| 2 | x |\n| 1,5 | x |\n| -3 | x |\n"
        "| +79991234567 | x |\n| 007 | x |\n| 1e5 | x |\n| nan | x |\n"
    )
    data, _, _ = render_document("xlsx", "t", md)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active
    col = [ws.cell(row=r, column=1).value for r in range(2, 9)]
    assert col == [2, 1.5, -3, "+79991234567", "007", "1e5", "nan"]


def test_render_csv_rows() -> None:
    data, _, _ = render_document("csv", TITLE, MD)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows == [
        ["Товар", "Кол-во"],
        ["Молоко", "2"],
        ["Хлеб", "1"],
    ]


def test_render_docx_structure() -> None:
    import docx

    data, _, _ = render_document("docx", TITLE, MD)
    document = docx.Document(io.BytesIO(data))
    styles = {p.style.name for p in document.paragraphs}
    assert any(s.startswith("Heading") or s.startswith("Заголовок") for s in styles)
    assert len(document.tables) == 1
    assert len(document.tables[0].rows) == 3


def test_render_pptx_slides() -> None:
    import pptx

    data, _, _ = render_document("pptx", TITLE, MD)
    prs = pptx.Presentation(io.BytesIO(data))
    assert len(prs.slides._sldIdLst) >= 2


def test_render_pdf_pages() -> None:
    import pymupdf

    data, _, _ = render_document("pdf", TITLE, MD)
    doc = pymupdf.open(stream=data, filetype="pdf")
    assert doc.page_count >= 1
    doc.close()

    long_md = "\n\n".join(f"Абзац {i} и текст." for i in range(300))
    data, _, _ = render_document("pdf", TITLE, long_md)
    doc = pymupdf.open(stream=data, filetype="pdf")
    assert doc.page_count > 1
    doc.close()


def test_filename_sanitized() -> None:
    _, _, name = render_document("md", 'a/b\\c:d*e?f"g<h>i|', "x")
    assert name == "abcdefghi.md"
    _, _, name = render_document("md", "", "x")
    assert name == "document.md"


def test_unknown_format() -> None:
    with pytest.raises(ValueError):
        render_document("exe", TITLE, MD)


async def _make_conv(client: httpx.AsyncClient, token: str) -> int:
    resp = await client.post(
        "/conversations",
        json={"kind": "personal", "title": "t", "member_ids": []},
        headers=auth(token),
    )
    assert resp.status_code == 201
    return int(resp.json()["id"])


async def test_e2e_create_document(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """Chat model calls create_document; the reply gets a document part."""
    calls = {"n": 0}

    async def scripted_stream(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {
                0: DeltaToolCall(
                    name="create_document",
                    json_args=json.dumps(
                        {"format": "xlsx", "title": TITLE, "content": MD}
                    ),
                )
            }
        else:
            yield "Готово"

    real_get_model = providers.get_model

    async def fake_get_model(session, capability, user):
        if capability == "chat":
            return BoundModel(
                FunctionModel(stream_function=scripted_stream),
                "test/scripted",
                ModelSettings(),
            )
        return await real_get_model(session, capability, user)

    monkeypatch.setattr(providers, "get_model", fake_get_model)

    await make_user("doc_e2e", "adult")
    token = await login(client, "doc_e2e")
    cid = await _make_conv(client, token)
    resp = await client.post(
        f"/conversations/{cid}/messages",
        json={"parts": [{"type": "text", "text": "список покупок в xlsx"}]},
        headers=auth(token),
    )
    assert resp.status_code == 201
    await get_runtime().wait_idle()

    async with db.get_sessionmaker()() as session:
        run = (
            await session.execute(select(Run).where(Run.conversation_id == cid))
        ).scalar_one()
        assert run.status == "done"
        reply = (
            await session.execute(
                select(Message).where(Message.run_id == run.id)
            )
        ).scalar_one()
    doc_parts = [p for p in reply.parts if p.get("type") == "document"]
    assert len(doc_parts) == 1
    part = doc_parts[0]
    assert part["name"] == f"{TITLE}.xlsx"
    assert part["generated"] is True

    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, part["attachment_id"])
        assert att is not None and att.derived.get("generated") is True
        data = media_service.read_blob(att.sha256)
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert "Молоко" in "\n".join(
        str(c.value) for row in wb.active.iter_rows() for c in row
    )

    # The member can download it.
    got = await client.get(
        f"/attachments/{att.id}", headers=auth(token)
    )
    assert got.status_code == 200
    assert got.content == data


async def test_document_writer_model(engine) -> None:
    """Bound `document` capability rewrites the brief into final content."""
    await _add_provider(
        "document",
        {"custom_output_text": "| Товар | Кол-во |\n|---|---|\n| Сметана | 1 |"},
    )
    user = await make_user("doc_writer", "adult")
    async with db.get_sessionmaker()() as session:
        conv = await conversations.create_conversation(
            session, user, "personal", "t", []
        )
        await session.commit()
        conv_id = conv.id
    ctx = SimpleNamespace(
        deps=ToolDeps(user_id=user.id, conversation_id=conv_id)
    )
    result = await create_document(
        ctx, format="xlsx", title="Покупки", content="молоко и хлеб"
    )
    assert result["attachment_id"]
    async with db.get_sessionmaker()() as session:
        att = await session.get(Attachment, result["attachment_id"])
        blob = media_service.read_blob(att.sha256)
        text = att.derived["text"]
    assert "Сметана" in text
    assert "хлеб" not in text
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(blob))
    assert any(
        "Сметана" in str(c.value)
        for row in wb.active.iter_rows()
        for c in row
    )


async def test_telegram_sends_document(engine) -> None:
    """An assistant document part reaches Telegram via send_document."""
    user = await make_user("doc_tg", "adult")
    async with db.get_sessionmaker()() as session:
        att, _ = await media_service.create_attachment(
            session,
            user,
            "Список покупок.xlsx",
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet",
            render_document("xlsx", TITLE, MD)[0],
            None,
        )
        att.derived = {"status": "done", "generated": True}
        await session.commit()
        att_id = att.id

    hub = get_hub()
    outbox = FakeOutbox()
    conn = hub.connect(TelegramSink(outbox, 100, 0))
    await hub.subscribe(conn, 555)
    try:
        await hub.publish(
            ChannelEvent(
                "message.created",
                555,
                {
                    "message": {
                        "id": 1,
                        "author_kind": "assistant",
                        "parts": [
                            {"type": "text", "text": "Держи файл"},
                            {
                                "type": "document",
                                "attachment_id": att_id,
                                "name": "Список покупок.xlsx",
                                "generated": True,
                            },
                        ],
                        "run_id": 1,
                        "source_channel": "web",
                    }
                },
            )
        )
        docs = [c for c in outbox.calls if c[0] == "document"]
        assert len(docs) == 1
        assert docs[0][2] == "Список покупок.xlsx"
        assert docs[0][3] > 0  # byte length sent
    finally:
        await hub.disconnect(conn)
