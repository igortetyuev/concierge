"""Telegram message formatting: MarkdownV2 escaping and splitting."""

import re

MDV2_SPECIALS = re.compile(r"([_*[\]()~`>#+\-=|{}.!\\])")
MAX_LEN = 4096

_TOKEN = re.compile(
    r"(```[\s\S]*?```|`[^`\n]+`|\*\*[^*]+\*\*|__[^_]+__"
    r"|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^)\n]+\))"
)
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")


def escape_mdv2(text: str) -> str:
    return MDV2_SPECIALS.sub(r"\\\1", text)


def _escape_code(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _convert(token: str) -> str:
    if token.startswith("```"):
        return f"```{_escape_code(token[3:-3])}```"
    if token.startswith("`"):
        return f"`{_escape_code(token[1:-1])}`"
    if token.startswith("**") or token.startswith("__"):
        return f"*{md_to_mdv2(token[2:-2])}*"
    if token.startswith("*") or token.startswith("_"):
        return f"_{md_to_mdv2(token[1:-1])}_"
    m = _LINK.fullmatch(token)
    if m:
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return f"[{md_to_mdv2(m.group(1))}]({url})"
    return escape_mdv2(token)


def md_to_mdv2(text: str) -> str:
    """Best-effort markdown -> Telegram MarkdownV2."""
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        out.append(escape_mdv2(text[pos : m.start()]))
        out.append(_convert(m.group(0)))
        pos = m.end()
    out.append(escape_mdv2(text[pos:]))
    return "".join(out)


def split_telegram(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split on newlines where possible; hard-split long chunks."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]
