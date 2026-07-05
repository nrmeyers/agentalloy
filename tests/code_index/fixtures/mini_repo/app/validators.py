"""Record validation rules."""


def validate_record(record):
    """Check that an order record has an id and a positive quantity."""
    if not record.get("id"):
        raise ValueError("record is missing an id")
    if record.get("quantity", 0) <= 0:
        raise ValueError("quantity must be positive")
    return True
