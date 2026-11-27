# ruff: noqa: RUF001, RUF003
"""Deterministic purchase-unit normalization and conversion.

The purchase domain accepts open-ended units: unknown values are preserved as
custom units instead of being rejected. Conversion exists only for the small
metric dimensions where it is unambiguous.
"""

from decimal import Decimal, InvalidOperation

QuantityInput = int | float | str | Decimal

# Keys are already lowercase with ё folded to е (see `_clean`).
_ALIASES = {
    "шт": "шт",
    "шт.": "шт",
    "штука": "шт",
    "штуки": "шт",
    "штук": "шт",
    "г": "г",
    "гр": "г",
    "гр.": "г",
    "грамм": "г",
    "грамма": "г",
    "граммы": "г",
    "граммов": "г",
    "g": "г",
    "kg": "кг",
    "кг": "кг",
    "килограмм": "кг",
    "килограмма": "кг",
    "килограммы": "кг",
    "килограммов": "кг",
    "кило": "кг",
    "мл": "мл",
    "мл.": "мл",
    "миллилитр": "мл",
    "миллилитра": "мл",
    "миллилитры": "мл",
    "миллилитров": "мл",
    "ml": "мл",
    "л": "л",
    "л.": "л",
    "литр": "л",
    "литра": "л",
    "литры": "л",
    "литров": "л",
    "l": "л",
    "мм": "мм",
    "мм.": "мм",
    "миллиметр": "мм",
    "миллиметра": "мм",
    "миллиметры": "мм",
    "миллиметров": "мм",
    "mm": "мм",
    "см": "см",
    "см.": "см",
    "сантиметр": "см",
    "сантиметра": "см",
    "сантиметры": "см",
    "сантиметров": "см",
    "cm": "см",
    "м": "м",
    "м.": "м",
    "метр": "м",
    "метра": "м",
    "метры": "м",
    "метров": "м",
    "m": "м",
    "бутылка": "бутылка",
    "бутылки": "бутылка",
    "бутылок": "бутылка",
    "упаковка": "упаковка",
    "упаковки": "упаковка",
    "упаковок": "упаковка",
    "пачка": "пачка",
    "пачки": "пачка",
    "пачек": "пачка",
    "коробка": "коробка",
    "коробки": "коробка",
    "коробок": "коробка",
    "банка": "банка",
    "банки": "банка",
    "банок": "банка",
    "пакет": "пакет",
    "пакета": "пакет",
    "пакеты": "пакет",
    "пакетов": "пакет",
    "рулон": "рулон",
    "рулона": "рулон",
    "рулоны": "рулон",
    "рулонов": "рулон",
    "тюбик": "тюбик",
    "тюбика": "тюбик",
    "тюбики": "тюбик",
    "тюбиков": "тюбик",
}

_PACKAGE_UNITS = {
    "бутылка",
    "упаковка",
    "пачка",
    "коробка",
    "банка",
    "пакет",
    "рулон",
    "тюбик",
}

_DIMENSIONS = {
    "шт": "count",
    "г": "mass",
    "кг": "mass",
    "мл": "volume",
    "л": "volume",
    "мм": "length",
    "см": "length",
    "м": "length",
    **{unit: "count" for unit in _PACKAGE_UNITS},
}

# Multipliers to the base unit of convertible metric dimensions.
_FACTORS = {
    "mass": {"г": Decimal("1"), "кг": Decimal("1000")},
    "volume": {"мл": Decimal("1"), "л": Decimal("1000")},
    "length": {
        "мм": Decimal("1"),
        "см": Decimal("10"),
        "м": Decimal("1000"),
    },
}


def _clean(unit: str) -> str:
    return " ".join(unit.strip().casefold().replace("ё", "е").split())


def normalize_unit(unit: str | None) -> str:
    """Canonical display unit; unknown units are preserved in normalized form."""
    if unit is None or not unit.strip():
        return "шт"
    cleaned = _clean(unit)
    return _ALIASES.get(cleaned, cleaned)


def unit_dimension(unit: str | None) -> str:
    """Conversion/compatibility dimension for a unit.

    Packaging units are count-like but intentionally have no conversion factors:
    a bottle, box or pack is not a deterministic number of pieces. Unknown units
    get their own `unit:<name>` dimension, so unrelated custom units never merge.
    """
    canonical = normalize_unit(unit)
    return _DIMENSIONS.get(canonical, f"unit:{canonical}")


def quantity_decimal(value: QuantityInput | None, default: Decimal = Decimal("1")) -> Decimal:
    """Parse a practical API quantity into Decimal without float artifacts."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        out = value
    else:
        try:
            out = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"bad quantity {value!r}") from None
    if not out.is_finite():
        raise ValueError(f"bad quantity {value!r}")
    return out


def convert_quantity(
    quantity: QuantityInput, from_unit: str | None, to_unit: str | None
) -> Decimal | None:
    """Convert inside one metric dimension; None means incompatible units."""
    value = quantity_decimal(quantity)
    source = normalize_unit(from_unit)
    target = normalize_unit(to_unit)
    if source == target:
        return value
    source_dim = unit_dimension(source)
    if source_dim != unit_dimension(target):
        return None
    factors = _FACTORS.get(source_dim)
    if factors is None or source not in factors or target not in factors:
        return None
    return value * factors[source] / factors[target]


def units_mergeable(existing_unit: str | None, incoming_unit: str | None) -> bool:
    """Whether two units can be represented in the existing item's unit."""
    return (
        convert_quantity(Decimal("1"), incoming_unit, existing_unit) is not None
    )


def format_quantity(quantity: Decimal | int | float | str) -> str:
    """Compact decimal for markdown/spoken output (no binary float artifacts)."""
    value = quantity_decimal(quantity)
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def practical_number(value: Decimal | None) -> int | float | None:
    """JSON-friendly numeric value used in structured query results."""
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value.normalize())
