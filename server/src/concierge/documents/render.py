"""Render generated documents (md/txt/csv/docx/xlsx/pptx/pdf) from Markdown."""

import csv
import html
import io
import re
from dataclasses import dataclass, field
from typing import Any

FORMATS = ("md", "txt", "csv", "docx", "xlsx", "pptx", "pdf")

MIMES = {
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument."
    "presentationml.presentation",
    "pdf": "application/pdf",
}

CONTENT_MAX_CHARS = 200_000
_FILENAME_BAD = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")

_PDF_CSS = """
body { font-family: sans-serif; font-size: 11pt; }
table { border-collapse: collapse; }
th, td { border: 1px solid #444; padding: 4px 8px; }
pre { font-family: monospace; }
blockquote { color: #555; }
"""


def render_document(fmt: str, title: str, content: str) -> tuple[bytes, str, str]:
    """-> (data, mime, filename). `content` is Markdown (GFM pipe tables)."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown document format {fmt!r}")
    content = content[:CONTENT_MAX_CHARS]
    filename = f"{_safe_name(title)}.{fmt}"
    if fmt in ("md", "txt"):
        data = _text(fmt, title, content).encode("utf-8")
    else:
        blocks = _blocks(content)
        if fmt == "csv":
            data = _csv(blocks, content)
        elif fmt == "xlsx":
            data = _xlsx(blocks, title, content)
        elif fmt == "docx":
            data = _docx(blocks, title)
        elif fmt == "pptx":
            data = _pptx(blocks, title)
        else:
            data = _pdf(title, content)
    return data, MIMES[fmt], filename


def _safe_name(title: str) -> str:
    name = _WS.sub(" ", _FILENAME_BAD.sub("", title)).strip()[:100].strip()
    return name or "document"


def _has_heading(content: str) -> bool:
    return bool(re.match(r"^#{1,6}\s", content.lstrip()))


def _text(fmt: str, title: str, content: str) -> str:
    if _has_heading(content):
        return content
    head = f"# {title}\n\n" if fmt == "md" else f"{title}\n\n"
    return head + content


# ---------- markdown block model ----------


@dataclass(slots=True)
class Inline:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass(slots=True)
class Block:
    kind: str  # heading | paragraph | item | table | code | quote
    runs: list[Inline] = field(default_factory=list)
    level: int = 0  # heading level or list nesting depth
    ordered: bool = False
    rows: list[list[str]] = field(default_factory=list)  # table rows
    text: str = ""  # code content


def _md() -> Any:
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": False}).enable("table")


def _inline(children: list[Any] | None) -> list[Inline]:
    runs: list[Inline] = []
    bold = italic = False
    for t in children or []:
        if t.type == "strong_open":
            bold = True
        elif t.type == "strong_close":
            bold = False
        elif t.type == "em_open":
            italic = True
        elif t.type == "em_close":
            italic = False
        elif t.type == "code_inline":
            runs.append(Inline(t.content, bold, italic, code=True))
        elif t.type in ("text", "html_inline"):
            runs.append(Inline(t.content, bold, italic))
        elif t.type == "softbreak":
            runs.append(Inline(" ", bold, italic))
    return runs


def _plain(children: list[Any] | None) -> str:
    return "".join(r.text for r in _inline(children))


def _blocks(content: str) -> list[Block]:
    tokens = _md().parse(content)
    blocks: list[Block] = []
    lists: list[bool] = []  # ordered flag per open list
    quote = False
    item_depth: int | None = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.type == "heading_open":
            blocks.append(
                Block("heading", _inline(tokens[i + 1].children), int(t.tag[1:]))
            )
            i += 3
            continue
        if t.type == "paragraph_open":
            kind = "quote" if quote else "paragraph"
            if item_depth is not None and not quote:
                kind = "item"
            blocks.append(
                Block(
                    kind,
                    _inline(tokens[i + 1].children),
                    level=item_depth or 0,
                    ordered=lists[-1] if lists and kind == "item" else False,
                )
            )
            i += 3
            continue
        if t.type == "bullet_list_open":
            lists.append(False)
        elif t.type == "ordered_list_open":
            lists.append(True)
        elif t.type in ("bullet_list_close", "ordered_list_close"):
            if lists:
                lists.pop()
        elif t.type == "list_item_open":
            item_depth = len(lists) - 1
        elif t.type == "list_item_close":
            item_depth = None
        elif t.type == "blockquote_open":
            quote = True
        elif t.type == "blockquote_close":
            quote = False
        elif t.type in ("fence", "code_block"):
            blocks.append(Block("code", text=t.content))
        elif t.type == "table_open":
            rows: list[list[str]] = []
            j = i + 1
            while j < len(tokens) and tokens[j].type != "table_close":
                if tokens[j].type == "tr_open":
                    cells: list[str] = []
                    j += 1
                    while j < len(tokens) and tokens[j].type != "tr_close":
                        if tokens[j].type == "inline":
                            cells.append(_plain(tokens[j].children))
                        j += 1
                    rows.append(cells)
                j += 1
            blocks.append(Block("table", rows=rows))
            i = j
        i += 1
    return blocks


def _text_of(block: Block) -> str:
    return "".join(r.text for r in block.runs)


# ---------- csv ----------


def _csv_rows(blocks: list[Block], content: str) -> list[list[str]]:
    for b in blocks:
        if b.kind == "table" and b.rows:
            return b.rows
    try:
        return [r for r in csv.reader(io.StringIO(content)) if r]
    except csv.Error:
        return [[line] for line in content.splitlines() if line.strip()]


def _csv(blocks: list[Block], content: str) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(_csv_rows(blocks, content))
    return buf.getvalue().encode("utf-8-sig")


_NUM_RE = re.compile(r"-?(0|[1-9]\d*)([.,]\d+)?")


def _number(text: str) -> int | float | str:
    s = text.strip()
    if not _NUM_RE.fullmatch(s):
        return text
    if "." in s or "," in s:
        return float(s.replace(",", "."))
    return int(s)


# ---------- xlsx ----------


def _sheet_name(base: str, used: set[str]) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "", base).strip()[:31] or "Лист"
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{name[:28]} {n}"
        n += 1
    used.add(candidate)
    return candidate


def _xlsx(blocks: list[Block], title: str, content: str) -> bytes:
    import openpyxl  # type: ignore[import-untyped]
    from openpyxl.styles import Font  # type: ignore[import-untyped]

    wb = openpyxl.Workbook()
    wb.properties.title = title
    wb.remove(wb.active)
    used: set[str] = set()
    tables = [b for b in blocks if b.kind == "table" and b.rows]
    if not tables:
        tables = [Block("table", rows=_csv_rows(blocks, content))]
        headings: list[str] = []
    else:
        headings = []
        last_heading = ""
        for b in blocks:
            if b.kind == "heading":
                last_heading = _text_of(b)
            if b.kind == "table":
                headings.append(last_heading)
    for idx, table in enumerate(tables):
        if headings:
            base = headings[idx] or (title if idx == 0 else f"Лист {idx + 1}")
        else:
            base = title if idx == 0 else f"Лист {idx + 1}"
        ws = wb.create_sheet(_sheet_name(base, used))
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row):
                value = cell if r == 0 else _number(cell)
                xcell = ws.cell(row=r + 1, column=c + 1, value=value)
                if r == 0:
                    xcell.font = Font(bold=True)
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(
                max(width + 2, 8), 60
            )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- docx ----------


def _docx_runs(paragraph: Any, runs: list[Inline]) -> None:
    for r in runs:
        run = paragraph.add_run(r.text)
        run.bold = r.bold or None
        run.italic = r.italic or None
        if r.code:
            run.font.name = "Courier New"


def _docx(blocks: list[Block], title: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_heading(title, 0)
    for b in blocks:
        text = _text_of(b)
        if b.kind == "heading":
            document.add_heading(text, min(max(b.level, 1), 9))
        elif b.kind == "paragraph":
            _docx_runs(document.add_paragraph(), b.runs)
        elif b.kind == "item":
            base = "List Number" if b.ordered else "List Bullet"
            style = f"{base} {b.level + 1}" if b.level else base
            try:
                p = document.add_paragraph(style=style)
            except KeyError:
                p = document.add_paragraph(style=base)
            _docx_runs(p, b.runs)
        elif b.kind == "table" and b.rows:
            cols = max(len(r) for r in b.rows)
            table = document.add_table(
                rows=len(b.rows), cols=cols, style="Table Grid"
            )
            for r, row in enumerate(b.rows):
                for c, cell in enumerate(row):
                    tcell = table.rows[r].cells[c]
                    tcell.text = cell
                    if r == 0:
                        for p in tcell.paragraphs:
                            for run in p.runs:
                                run.bold = True
        elif b.kind == "code":
            p = document.add_paragraph()
            run = p.add_run(b.text.rstrip("\n"))
            run.font.name = "Courier New"
        elif b.kind == "quote":
            try:
                p = document.add_paragraph(style="Quote")
            except KeyError:
                p = document.add_paragraph()
            _docx_runs(p, b.runs)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# ---------- pptx ----------


def _pptx(blocks: list[Block], title: str) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title

    body = None

    def content_slide(slide_title: str) -> Any:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = slide_title
        return s.placeholders[1].text_frame

    def add_line(frame: Any, runs: list[Inline], level: int) -> None:
        p = frame.paragraphs[0] if frame.text == "" else frame.add_paragraph()
        p.level = min(level, 4)
        for r in runs:
            run = p.add_run()
            run.text = r.text
            run.font.bold = r.bold
            run.font.italic = r.italic
            if r.code:
                run.font.name = "Courier New"

    last_heading = ""
    for b in blocks:
        if b.kind == "heading" and b.level <= 2:
            last_heading = _text_of(b)
            body = content_slide(last_heading)
        elif b.kind in ("paragraph", "item", "quote"):
            if body is None:
                body = content_slide(title)
            add_line(body, b.runs, b.level if b.kind == "item" else 0)
        elif b.kind == "code":
            if body is None:
                body = content_slide(title)
            add_line(body, [Inline(b.text.rstrip("\n"), code=True)], 0)
        elif b.kind == "table" and b.rows:
            s = prs.slides.add_slide(prs.slide_layouts[5])
            s.shapes.title.text = last_heading or title
            rows, cols = len(b.rows), max(len(r) for r in b.rows)
            gt = s.shapes.add_table(
                rows, cols, Inches(0.5), Inches(1.5), Inches(9), Inches(1)
            ).table
            for r, row in enumerate(b.rows):
                for c, cell in enumerate(row):
                    tcell = gt.cell(r, c)
                    tcell.text = cell
                    for p in tcell.text_frame.paragraphs:
                        for run in p.runs:
                            run.font.size = Pt(14)
                            if r == 0:
                                run.font.bold = True
            body = None
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ---------- pdf ----------


def _pdf(title: str, content: str) -> bytes:
    import pymupdf

    body = _md().render(content)
    head = "" if _has_heading(content) else f"<h1>{html.escape(title)}</h1>"
    story: Any = pymupdf.Story(  # type: ignore[no-untyped-call]
        html=head + body, user_css=_PDF_CSS
    )
    buf = io.BytesIO()
    writer: Any = pymupdf.DocumentWriter(  # type: ignore[no-untyped-call]
        buf
    )
    mediabox = pymupdf.paper_rect("a4")
    where = pymupdf.Rect(  # type: ignore[no-untyped-call]
        36, 36, mediabox.width - 36, mediabox.height - 36
    )
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()
