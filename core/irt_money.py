"""IRT/Rial display helpers. 1 Toman = 10 Rial."""

_TOMAN_PER_RIAL = 0.1


def quote_uses_toman(quote: str) -> bool:
    return str(quote or "").upper() in {"IRT", "RLS", "IRR"}


def rial_to_toman(value: float) -> float:
    return float(value) * _TOMAN_PER_RIAL


def toman_to_rial(value: float) -> float:
    return float(value) / _TOMAN_PER_RIAL


def parse_amount(text: str, default: float = 0.0) -> float:
    try:
        return float(str(text).replace(",", "").strip() or default)
    except (TypeError, ValueError):
        return float(default)
