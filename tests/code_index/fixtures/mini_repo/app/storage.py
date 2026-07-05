"""Very small JSON-lines persistence layer."""

import json

from app.validators import validate_record


def save_record(path, record):
    """Validate then append an order record to the quxglobber ledger file."""
    validate_record(record)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def load_records(path):
    """Read every order record back from the ledger file."""
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            records.append(json.loads(line))
    return records
