# ruff: noqa: RUF001
"""Deterministic markdown and spoken rendering for purchase query results."""

from concierge.purchases.service import ItemView, ListView, QueryResult
from concierge.purchases.units import format_quantity

_UNIT_FORMS = {
    "шт": ("штука", "штуки", "штук"),
    "г": ("грамм", "грамма", "граммов"),
    "кг": ("килограмм", "килограмма", "килограммов"),
    "мл": ("миллилитр", "миллилитра", "миллилитров"),
    "л": ("литр", "литра", "литров"),
    "мм": ("миллиметр", "миллиметра", "миллиметров"),
    "см": ("сантиметр", "сантиметра", "сантиметров"),
    "м": ("метр", "метра", "метров"),
    "бутылка": ("бутылка", "бутылки", "бутылок"),
    "упаковка": ("упаковка", "упаковки", "упаковок"),
    "пачка": ("пачка", "пачки", "пачек"),
    "коробка": ("коробка", "коробки", "коробок"),
    "банка": ("банка", "банки", "банок"),
    "пакет": ("пакет", "пакета", "пакетов"),
    "рулон": ("рулон", "рулона", "рулонов"),
    "тюбик": ("тюбик", "тюбика", "тюбиков"),
}


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    value = abs(n) % 100
    if 11 <= value <= 19:
        return forms[2]
    value %= 10
    if value == 1:
        return forms[0]
    if 2 <= value <= 4:
        return forms[1]
    return forms[2]


def _quantity_text(item: ItemView, spoken: bool) -> str:
    qty = format_quantity(item.quantity)
    if not spoken:
        return f"{qty} {item.unit}"
    if item.unit == "шт" and item.quantity == 1:
        return ""
    forms = _UNIT_FORMS.get(item.unit)
    if forms is None:
        unit_text = item.unit
    elif isinstance(item.quantity, int):
        unit_text = _plural(item.quantity, forms)
    else:
        # Practical Russian for fractional quantities: «1,5 литра/килограмма».
        unit_text = forms[1]
    return f"{qty} {unit_text}"


def _item_text(item: ItemView, *, spoken: bool = False) -> str:
    quantity = _quantity_text(item, spoken)
    return f"{quantity} {item.name}".strip() if quantity else item.name


def _markdown_line(item: ItemView) -> str:
    checked = item.status == "bought"
    parts = [_item_text(item)]
    if item.category != "прочее":
        parts.append(f"категория: {item.category}")
    if item.assignee_name:
        parts.append(f"назначено: {item.assignee_name}")
    if item.price_amount is not None:
        price = format_quantity(item.price_amount)
        kind = {"unit": "за единицу", "total": "итого"}.get(item.price_kind or "", "")
        parts.append(f"{price} {item.price_currency}{f' {kind}' if kind else ''}")
    return f"- [{'x' if checked else ' '}] {'; '.join(parts)}"


def render_markdown(result: QueryResult) -> str:
    """Markdown grouped by list, with stable pending/bought sections."""
    lines = ["# Покупки"]
    if not result.lists:
        lines.extend(["", "Списки пусты."])
        return "\n".join(lines)
    for plist in result.lists:
        lines.extend(["", f"## {plist.title} — {plist.owner_name}", ""])
        pending = [item for item in plist.items if item.status == "pending"]
        bought = [item for item in plist.items if item.status == "bought"]
        lines.append("### Осталось купить")
        if pending:
            lines.extend(_markdown_line(item) for item in pending)
        else:
            lines.append("—")
        lines.extend(["", "### Уже купили"])
        if bought:
            lines.extend(_markdown_line(item) for item in bought)
        else:
            lines.append("—")
    return "\n".join(lines)


def _join_words(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    if len(words) == 2:
        return f"{words[0]} и {words[1]}"
    return ", ".join(words[:-1]) + " и " + words[-1]


def _place(plist: ListView) -> str:
    if not plist.store_key:
        return "В общем списке"
    name = plist.store_name or plist.store_key
    # A small practical prepositional form for common Russian store names;
    # unknown/indeclinable names are simply used as-is.
    lower = name.casefold()
    if lower.endswith(("ка", "та", "а")):
        spoken = name[:-1] + "е"
    elif lower.endswith("т") and any("а" <= ch <= "я" or ch == "ё" for ch in lower):
        spoken = name + "е"
    else:
        spoken = name
    return f"В {spoken}"


def _spoken_place(plist: ListView, include_owner: bool) -> str:
    place = _place(plist)
    if not include_owner:
        return place
    owner = plist.owner_name
    if not plist.store_key:
        return f"В общем списке «{owner}»"
    return f"В списке «{owner}» {place[:1].lower()}{place[1:]}"


def _spoken_list(plist: ListView, include_owner: bool = False) -> list[str]:
    place = _spoken_place(plist, include_owner)
    pending = [item for item in plist.items if item.status == "pending"]
    bought = [item for item in plist.items if item.status == "bought"]
    sentences: list[str] = []
    if bought:
        bought_text = _join_words([_item_text(i, spoken=True) for i in bought])
        sentences.append(f"{place} уже купили {bought_text}.")
    if pending:
        pending_text = _join_words([_item_text(i, spoken=True) for i in pending])
        sentences.append(f"{place} осталось купить {pending_text}.")
    return sentences


def render_spoken(result: QueryResult) -> str:
    """Concise conversational summary; morphology is practical, not perfect."""
    multi_owner = len({plist.owner_id for plist in result.lists}) > 1
    sentences = [
        sentence
        for plist in result.lists
        for sentence in _spoken_list(plist, include_owner=multi_owner)
    ]
    if not sentences:
        return "Списки покупок пусты."
    return " ".join(sentences)
