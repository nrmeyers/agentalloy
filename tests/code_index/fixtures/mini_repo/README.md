# mini_repo

A tiny order-processing service used as a code-index integration fixture.

## Architecture

- `app/config.py` parses `key=value` configuration files into settings.
- `app/validators.py` checks order records before anything touches them.
- `app/pricing.py` computes order totals and applies discounts.
- `app/storage.py` persists validated records to a JSON-lines ledger.
- `app/service.py` is the entry point tying the pieces together.
