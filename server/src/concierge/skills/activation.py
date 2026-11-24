"""Slash-command parsing and the active-skill prompt chunk."""

from typing import Any

from concierge.skills.format import body_of, render_body
from concierge.skills.models import Skill

# Compatibility lexicon for bodies written for Claude Code / Hermes / Devin:
# foreign tool names are mapped onto our own instead of registering aliases.
SKILL_COMPAT_PREAMBLE = (
    "Совместимость команд скилла:\n"
    "- AskUserQuestion / ask_user_question / clarify — используй инструмент "
    "`ask_user_question`.\n"
    "- Task / Agent / run_subagent / delegate_task / sidekick — используй "
    "`run_subagent`; параллельная пачка субагентов = несколько вызовов в "
    "одном ответе.\n"
    "- Read / skill_view файла скилла, пути вида ${CLAUDE_SKILL_DIR}/x — "
    "используй `read_skill_file` с путём «x».\n"  # noqa: RUF001
    "- TodoWrite — веди список задач прямо в тексте ответа.\n"
    "- Bash / Edit / Write / терминал недоступны: если нужно изменение "
    "кода — `delegate_coding_task` (если доступен), иначе скажи "
    "пользователю.\n"
    "- EnterPlanMode / ExitPlanMode — игнорируй."
)

SkillCommand = tuple[str, str, str]  # ("stop", "", "") | ("skill", token, args)


def parse_command(parts: list[dict[str, Any]]) -> SkillCommand | None:
    """A message is a skill command when its first text part starts with '/'."""
    first = next(
        (p for p in parts if p.get("type") == "text"), None
    )
    if first is None:
        return None
    text = str(first.get("text", "")).lstrip()
    if not text.startswith("/"):
        return None
    head, _, args = text[1:].partition(" ")
    token = head.split("@", 1)[0].lower()
    if not token:
        return None
    if token == "stop":
        return ("stop", "", "")
    return ("skill", token, args.strip())


def active_skill_chunk(skill: Skill, args: str) -> str:
    """The `[Активный скилл]` instructions chunk injected per run."""
    body = render_body(body_of(skill.content), args, skill.name)
    return (
        f"[Активный скилл: {skill.name}]\n{SKILL_COMPAT_PREAMBLE}\n\n{body}"
    )
