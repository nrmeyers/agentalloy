## Pattern B: Silent JSON-decode → whole-file overwrite

### Bug
Two code paths caught `json.JSONDecodeError` during JSON file reads and silently set the parsed data to `{}`, then overwrote the entire file with empty settings, causing data loss:
- `providers/cline/install.py:50`: `except json.JSONDecodeError: settings = {}`
- `wire_harness.py:1258`: Same pattern in `_wire_proxy_cline()`

### Fix
- Print warning to stderr and return early (empty list)
- Leave original file untouched on decode error
- Added tests: `test_cline_provider.py` (17 tests), `test_wire_harness.py` (2 new tests)

18 additional `except json.JSONDecodeError` sites were audited — all others either raise `SystemExit`, return early with warning, skip without writing, or return error dicts without file write-back.

4 files changed, 369 insertions(+), 2 deletions(-)