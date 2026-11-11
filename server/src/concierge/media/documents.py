"""Extract text from office/PDF/plain documents into attachments.derived."""

import csv
import io
import logging
from typing import Any

log = logging.getLogger(__name__)

TEXT_MAX_CHARS = 200_000

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)


def extract_document(mime: str, name: str, data: bytes) -> dict[str, Any]:
    """-> {"text": ..., "pages": n} where pages is a page/sheet/slide count."""
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if mime == "application/pdf" or suffix == "pdf":
        return _pdf(data)
    if mime == _DOCX_MIME or suffix == "docx":
        return _docx(data)
    if mime in (_XLSX_MIME, "application/vnd.ms-excel") or suffix in (
        "xlsx",
        "xls",
    ):
        return _xlsx(data)
    if mime in (_PPTX_MIME, "application/vnd.ms-powerpoint") or suffix in (
        "pptx",
        "ppt",
    ):
        return _pptx(data)
    return _plain(mime, data)


def _pdf(data: bytes) -> dict[str, Any]:
    import pymupdf

    doc: Any = pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
    try:
        pages = doc.page_count
        text = "\n\n".join(doc[i].get_text() for i in range(pages))
    finally:
        doc.close()
    return {"text": text[:TEXT_MAX_CHARS], "pages": pages}


def _docx(data: bytes) -> dict[str, Any]:
    import docx

    document: Any = docx.Document(io.BytesIO(data))
    lines = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(c.text for c in row.cells))
    return {"text": "\n".join(lines)[:TEXT_MAX_CHARS]}


def _xlsx(data: bytes) -> dict[str, Any]:
    import openpyxl  # type: ignore[import-untyped]

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        out: list[str] = []
        for sheet in wb.worksheets:
            out.append(f"### {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(cells):
                    out.append("\t".join(cells))
        return {
            "text": "\n".join(out)[:TEXT_MAX_CHARS],
            "pages": len(wb.sheetnames),
        }
    finally:
        wb.close()


def _pptx(data: bytes) -> dict[str, Any]:
    import pptx

    prs = pptx.Presentation(io.BytesIO(data))
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [
            shape.text_frame.text
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        if texts:
            out.append(f"### Слайд {i}")
            out.extend(texts)
    return {
        "text": "\n".join(out)[:TEXT_MAX_CHARS],
        "pages": len(prs.slides._sldIdLst),
    }


def _plain(mime: str, data: bytes) -> dict[str, Any]:
    if mime == "text/csv" or mime == "application/csv":
        try:
            rows = list(csv.reader(io.StringIO(data.decode("utf-8", "replace"))))
            text = "\n".join("\t".join(r) for r in rows)
            return {"text": text[:TEXT_MAX_CHARS]}
        except csv.Error:
            pass
    return {"text": data.decode("utf-8", "replace")[:TEXT_MAX_CHARS]}
