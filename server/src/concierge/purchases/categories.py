# ruff: noqa: RUF001
"""Small deterministic baseline category inference for common purchase items."""

from concierge.purchases.service import normalize_product_key

# Intentionally small and conservative: this is a fallback for obvious staples,
# not a full product classifier. More specific multiword needles come first.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "бытовая химия",
        (
            "зубная паста",
            "зубная щетка",
            "стиральный порошок",
            "порошок",
            "моющее средство",
            "мыло",
            "шампунь",
            "туалетная бумага",
            "губка",
        ),
    ),
    (
        "хлеб/выпечка",
        ("хлеб", "батон", "булка", "булочка", "лаваш", "выпечка"),
    ),
    (
        "молочные",
        (
            "молоко",
            "молоч",
            "сыр",
            "творог",
            "йогурт",
            "кефир",
            "сливки",
            "сметана",
        ),
    ),
    (
        "напитки",
        ("вода", "сок", "напиток", "чай", "кофе", "лимонад", "минералка"),
    ),
    (
        "овощи/фрукты",
        (
            "яблок",
            "банан",
            "апельсин",
            "мандарин",
            "груш",
            "лимон",
            "фрукт",
            "картоф",
            "помидор",
            "томат",
            "огур",
            "морков",
            "лук",
            "перец",
            "овощ",
        ),
    ),
)


def infer_category(name: str) -> str:
    """Best-effort baseline category; `прочее` when nothing obvious matches."""
    key = normalize_product_key(name)
    for category, needles in _KEYWORDS:
        if any(needle in key for needle in needles):
            return category
    return "прочее"
