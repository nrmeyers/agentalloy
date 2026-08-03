# Fix Contract Artifact Show

## Acceptance Criteria

1. **CLI command exists:** `agentalloy contract artifact-show <phase>/<slug>/<name>` prints raw artifact content to stdout.
2. **CLI flags accepted:** `--phase`, `--slug`, `--name` are accepted as alternatives to the positional triple.
3. **`--json` works:** When `--json` is passed to the `contract` group, output is structured JSON with `phase`, `slug`, `name`, `content`, `updated_at` fields.
4. **404 on missing:** When the artifact does not exist, the CLI prints an error to stderr and exits with code 1.
5. **Raw content by default:** Non-JSON output prints only the artifact body (no markdown framing, no header) so agents can consume it as context.
6. **Status filtering:** The HTTP request filters to `status='active'` by default (lifecycle-ready from #520).
7. **Tests pass:** Unit tests cover success, 404, and `--json` output paths.

## Approach

Add a dedicated single-artifact HTTP route, update the client to use it, and add the CLI verb.

### 1. HTTP route — `GET /state/artifact/{phase}/{slug}/{name}`

In `api/state_router.py`, add this route **between** the existing `GET /state/artifact` list route and the catch-all `GET /state/{kind}`:

```python
@router.get(
    "/artifact/{phase}/{slug}/{name}",
    response_model=ArtifactResponse,
    responses={404: {"description": "Artifact not found"}},
    summary="Get a single artifact by (phase, slug, name)",
)
async def get_artifact(
    phase: str,
    slug: str,
    name: str,
    store: DuckDBStateStore = Depends(get_repo_store),
) -> ArtifactResponse:
    """Fetch a single artifact by (phase, slug, name).

    Returns 404 when the artifact does not exist.  Filters to
    ``status='active'`` by default so the route is lifecycle-ready
    from issue #520 without a follow-up change.
    """
    row = await asyncio.to_thread(store.get_artifact, phase, slug, name, status="active")
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    # Convert datetime.updated_at to ISO string for pydantic validation
    if hasattr(row.get("updated_at"), "isoformat"):
        row = {**row, "updated_at": row["updated_at"].isoformat()}
    # Strip 'status' — it's returned by the store but not in ArtifactResponse
    cleaned = {k: v for k, v in row.items() if k in ArtifactResponse.model_fields}
    return ArtifactResponse(**cleaned)
```

**Why a new route:** Client-side filtering off the list endpoint wastes transfer. The store method `get_artifact()` already exists and does the precise lookup.

**Route ordering:** Must be declared before `/{kind}` catch-all so FastAPI matches it first.

### 2. StateClient — `get_artifact` update

Update `api/state_client.py` to call the new endpoint directly instead of list+filter:

```python
def get_artifact(self, phase: str, slug: str, name: str) -> dict[str, Any] | None:
    """Fetch a single artifact by (phase, slug, name), or None if absent."""
    path = f"/state/artifact/{urllib.parse.quote(phase, safe='')}/{urllib.parse.quote(slug, safe='')}/{urllib.parse.quote(name, safe='')}"
    req = urllib.request.Request(f"{self.base}{path}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise StateClientError(exc.read().decode()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise StateClientError(f"agentalloy service is not running ({exc})") from exc
```

### 3. CLI handler — `contract artifact-show`

In `install/subcommands/contract.py`, add:

- **Handler `_artifact_show`**: parses the triple positional `phase/slug/name` or `--phase/--slug/--name` flags, calls `client.get_artifact()`, prints raw content (or JSON with `--json`).
- **Parser**: add subparser `artifact-show` with positional `triple` arg (format `phase/slug/name`) and alternative `--phase/--slug/--name` flags (mutually exclusive with triple).
- **Dispatch**: register in `_HANDLERS` dict and update usage string in `_run`.

Output behavior:
- Non-JSON: print raw artifact body to stdout (no header, no framing)
- JSON: print `{"phase": ..., "slug": ..., "name": ..., "content": ..., "updated_at": ...}`

## Test Cases

1. **Happy path:** Given an existing artifact, `artifact-show` prints its content.
2. **404 path:** Given a non-existing artifact, `artifact-show` prints error to stderr and exits 1.
3. **JSON output:** `--json` flag produces structured JSON with all fields.
4. **Flag mode:** `--phase/--slug/--name` flags produce same result as positional triple.
5. **Service down:** When service is down, exits with code 1 and stderr mentions 'service'.
6. **Invalid triple format:** Triple with wrong number of parts produces error.
7. **Missing args:** Neither triple nor all three flags provided produces error.
8. **Status filtering:** Only `status='active'` artifacts are returned by default.
9. **Route priority:** The specific `/artifact/{phase}/{slug}/{name}` route takes priority over the `/{kind}` catch-all.
