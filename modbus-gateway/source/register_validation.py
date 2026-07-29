"""Validazione condivisa dei valori dei registri Modbus.

Gli holding register con data_type "float" viaggiano come float32 IEEE-754
a due word. Quelli con data_type "int" viaggiano come int32 con segno a due
word (vedi modbus_client.write_holding_register_32bit).
"""

from decimal import Decimal, InvalidOperation
FLOAT32_MAX = Decimal("3.4028235e38")
INT32_MAX = Decimal("2147483647")   # 2**31 - 1, range int32 con segno
INT32_MIN = Decimal("-2147483648")  # -2**31
FLOAT32_SIGNIFICANT_DIGITS = 7


def _decimal(value):
    if isinstance(value, bool):
        raise InvalidOperation
    result = Decimal(str(value).strip())
    if not result.is_finite():
        raise InvalidOperation
    return result


def _significant_digits(value: Decimal) -> int:
    digits = "".join(map(str, value.as_tuple().digits)).lstrip("0")
    return len(digits) or 1


def normalize_float32_value(value) -> float:
    """Rimuove il rumore di conversione da float32 mantenendo 7 cifre significative.

    Un valore Modbus come ``4587.456`` viene fisicamente rappresentato come
    float32 e, una volta riletto, Python lo espone ad esempio come
    ``4587.4560546875``. Sette cifre significative sono anche il limite
    accettato in input per i registri ``FLOAT``: questa normalizzazione rende
    quindi stabile il valore archiviato senza ridurre la precisione ammessa.
    """
    return float(format(float(value), f".{FLOAT32_SIGNIFICANT_DIGITS}g"))


def validate_value(tipo_registro: str, data_type: str | None, raw_value):
    """Restituisce ``(valido, messaggio, valore_normalizzato)``."""
    try:
        if tipo_registro == "co":
            value = _decimal(raw_value)
            if value != value.to_integral_value() or value not in (Decimal(0), Decimal(1)):
                return False, "Coil: il valore deve essere 0 o 1", None
            return True, "", int(value)

        if tipo_registro != "hr":
            return False, f"Tipo registro non scrivibile: {tipo_registro}", None
        if data_type not in {"int", "float"}:
            return False, "Holding Register: data_type non valido", None

        value = _decimal(raw_value)
        if data_type == "int":
            if value != value.to_integral_value():
                return False, "Holding Register int: non sono ammesse cifre decimali", None
            if value < INT32_MIN or value > INT32_MAX:
                return False, "Holding Register int: valore fuori dal range int32 (±2.147.483.647)", None
            return True, "", int(value)

        if abs(value) > FLOAT32_MAX:
            return False, "Holding Register: valore fuori dal range float32", None
        if _significant_digits(value) > FLOAT32_SIGNIFICANT_DIGITS:
            return False, "Holding Register float: massimo 7 cifre significative", None
        return True, "", float(value)
    except (InvalidOperation, ValueError, TypeError):
        return False, f"Valore non numerico: {raw_value}", None