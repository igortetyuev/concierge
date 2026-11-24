"""SKILL.md parsing (agentskills.io) and skill-body helpers.

Format: YAML frontmatter between `---` markers + Markdown body. Extra
extensions: `aliases` (Cyrillic /команды) and `triggers` (activation phrases).
Unknown frontmatter keys are kept in the stored text untouched.
"""

import io
import re
import shlex
import zipfile
from dataclasses import dataclass, field

import yaml

NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
ALIAS_RE = re.compile(r"^[\w-]{1,64}$")
RESERVED_NAMES = {
    "stop",
    "skills",
    "start",
    "new",
    "sessions",
    "archive",
    "voice",
    "help",
}

CONTENT_MAX = 64 * 1024
FILE_MAX = 256 * 1024
FILES_MAX = 30
TRIGGER_MIN_WORDS = 2
TRIGGER_MIN_CHARS = 6


class SkillFormatError(ValueError):
    """Invalid SKILL.md; the message is shown to the user (HTTP 422)."""


@dataclass
class SkillDoc:
    name: str
    description: str
    aliases: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    argument_hint: str | None = None
    ephemeral: bool = False
    body: str = ""
    content: str = ""


_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n?---[ \t]*\r?\n?", re.DOTALL)


def _validate_trigger(trigger: str) -> None:
    norm = normalize_phrase(trigger)
    words = norm.split()
    if len(words) < TRIGGER_MIN_WORDS and len(norm) < TRIGGER_MIN_CHARS:
        raise SkillFormatError(
            f"триггер слишком короткий (минимум {TRIGGER_MIN_WORDS} слова "
            f"или {TRIGGER_MIN_CHARS} символов): {trigger!r}"
        )


def body_of(content: str) -> str:
    """Markdown body of a stored SKILL.md (frontmatter stripped)."""
    m = _FM_RE.match(content)
    return content[m.end() :].strip("\n") if m else content


def parse_skill_md(text: str) -> SkillDoc:
    """Parse and validate SKILL.md; raise SkillFormatError on bad input."""
    if len(text.encode("utf-8")) > CONTENT_MAX:
        raise SkillFormatError("SKILL.md больше 64 КБ")
    m = _FM_RE.match(text)
    if m is None:
        raise SkillFormatError(
            "нет YAML-заголовка: файл должен начинаться с «---»"  # noqa: RUF001
        )
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise SkillFormatError(f"ошибка разбора YAML-заголовка: {e}") from e
    if not isinstance(meta, dict):
        raise SkillFormatError("YAML-заголовок должен быть объектом")
    body = text[m.end() :].strip("\n")

    name = str(meta.get("name") or "").strip()
    if not name:
        raise SkillFormatError("в заголовке нет обязательного поля name")
    if not NAME_RE.match(name):
        raise SkillFormatError(
            "name должен быть [a-z0-9-] длиной 1-64 символа"
        )
    if name in RESERVED_NAMES:
        raise SkillFormatError(f"имя {name!r} зарезервировано")

    description = str(meta.get("description") or "").strip()
    if not description:
        raise SkillFormatError(
            "в заголовке нет обязательного поля description"
        )

    def _str_list(key: str) -> list[str]:
        raw = meta.get(key) or []
        if not isinstance(raw, list):
            raise SkillFormatError(f"{key} должен быть списком строк")
        return [str(x).strip() for x in raw if str(x).strip()]

    aliases = _str_list("aliases")
    for alias in aliases:
        low = alias.lower()
        if not ALIAS_RE.match(low):
            raise SkillFormatError(
                f"alias {alias!r} должен быть [\\w-] длиной до 64 символов"
            )
        if low == name or low in RESERVED_NAMES:
            raise SkillFormatError(f"alias {alias!r} зарезервирован")

    triggers = _str_list("triggers")
    for trigger in triggers:
        _validate_trigger(trigger)

    hint = meta.get("argument-hint") or meta.get("argument_hint") or None
    ephemeral = meta.get("ephemeral", False)
    if not isinstance(ephemeral, bool):
        raise SkillFormatError("ephemeral должен быть true или false")
    return SkillDoc(
        name=name,
        description=description,
        aliases=[a.lower() for a in aliases],
        triggers=triggers,
        argument_hint=str(hint)[:256] if hint else None,
        ephemeral=ephemeral,
        body=body,
        content=text,
    )


_NON_WORD_RE = re.compile(r"[^\w\s]+|_+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def normalize_phrase(text: str) -> str:
    """lowercase, ё->е, punctuation -> space, collapse whitespace."""  # noqa: RUF002
    text = text.lower().replace("ё", "е")  # noqa: RUF001
    text = _NON_WORD_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


def match_trigger(phrases: list[str], text: str) -> bool:
    """A phrase matches when its normalized words appear as a contiguous
    sequence of whole words inside the normalized text."""
    hay = f" {normalize_phrase(text)} "
    for phrase in phrases:
        needle = normalize_phrase(phrase)
        if needle and f" {needle} " in hay:
            return True
    return False


_ARGUMENTS_N_RE = re.compile(r"\$ARGUMENTS\[(\d+)\]")
_DOLLAR_N_RE = re.compile(r"\$(\d+)")


def render_body(body: str, args: str, name: str) -> str:
    """Substitute $ARGUMENTS / $ARGUMENTS[N] / $N and ${CLAUDE_SKILL_DIR}."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    has_arguments = "$ARGUMENTS" in body
    out = body.replace("${CLAUDE_SKILL_DIR}", f"skill:{name}")
    out = _ARGUMENTS_N_RE.sub(
        lambda m: tokens[int(m.group(1))] if int(m.group(1)) < len(tokens) else "",
        out,
    )
    out = _DOLLAR_N_RE.sub(
        lambda m: (
            tokens[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(tokens) else ""
        ),
        out,
    )
    out = out.replace("$ARGUMENTS", args)
    if not has_arguments and args.strip():
        out = f"{out.rstrip()}\n\nARGUMENTS: {args}"
    return out


def valid_file_path(path: str) -> bool:
    """Relative path, no '..', no absolute/drive prefixes, not SKILL.md."""
    if not path or path != path.strip():
        return False
    if path.startswith(("/", "\\")) or ":" in path.split("/", 1)[0]:
        return False
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return False
    if len(parts) == 1 and parts[0].upper() == "SKILL.MD":
        return False
    return len("/".join(parts)) <= 256


def unpack_archive(
    data: bytes, filename: str
) -> tuple[str, dict[str, str], list[str]]:
    """(skill_md_text, files, skipped) from a .md or a .zip archive.

    The zip may carry SKILL.md at its root or inside a single top-level
    directory; binary members (not decodable as UTF-8) are skipped and
    reported.
    """
    lower = filename.lower()
    if lower.endswith(".md"):
        return data.decode("utf-8"), {}, []
    if not lower.endswith(".zip"):
        raise SkillFormatError("ожидается .md или .zip архив")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SkillFormatError("битый zip-архив") from e
    entries = [i for i in zf.infolist() if not i.is_dir()]
    root = ""
    skill_md = None
    for info in entries:
        parts = [p for p in info.filename.split("/") if p]
        if len(parts) == 1 and parts[0].upper() == "SKILL.MD":
            skill_md, root = info, ""
            break
        if (
            len(parts) == 2
            and parts[1].upper() == "SKILL.MD"
            and not parts[0].startswith(".")
            and root in ("", parts[0])
        ):
            skill_md, root = info, parts[0]
    if skill_md is None:
        raise SkillFormatError("в архиве нет SKILL.md")
    try:
        content = zf.read(skill_md).decode("utf-8")
    except UnicodeDecodeError as e:
        raise SkillFormatError("SKILL.md не в UTF-8") from e
    files: dict[str, str] = {}
    skipped: list[str] = []
    prefix = f"{root}/" if root else ""
    for info in entries:
        if info is skill_md:
            continue
        if root and not info.filename.startswith(prefix):
            continue
        rel = info.filename[len(prefix) :]
        if not valid_file_path(rel):
            skipped.append(info.filename)
            continue
        try:
            files[rel] = zf.read(info).decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(info.filename)
    return content, files, skipped


def pack_archive(
    name: str, content: str, files: dict[str, str]
) -> bytes:
    """ZIP with `<name>/SKILL.md` and the skill's files under the same dir."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/SKILL.md", content)
        for path, text in files.items():
            zf.writestr(f"{name}/{path}", text)
    return buf.getvalue()
