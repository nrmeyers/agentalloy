"""Order-processing entry point wiring config, validation, pricing, storage."""

from app.config import load_config
from app.pricing import compute_total
from app.storage import save_record
from app.validators import validate_record


def process_order(config_path, ledger_path, record):
    """Validate an order, price it, and persist it to the ledger."""
    settings = load_config(config_path)
    validate_record(record)
    record = dict(record)
    record["total"] = compute_total(record, float(settings.get("discount_rate", 0.0)))
    save_record(ledger_path, record)
    return record
