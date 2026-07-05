"""Order pricing helpers."""


def apply_discount(total, rate):
    """Apply a fractional discount rate to a total."""
    return total * (1.0 - rate)


def compute_total(record, discount_rate=0.0):
    """Compute the price of an order record, discount included."""
    total = record.get("quantity", 0) * record.get("unit_price", 0.0)
    if discount_rate:
        total = apply_discount(total, discount_rate)
    return total
