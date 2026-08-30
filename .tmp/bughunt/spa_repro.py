"""Minimal repro of the spa.py catch-all traversal logic, using the project venv.

Mirrors mount_web_ui's catch-all exactly:
    file_path = dist / full_path
    if full_path and file_path.is_file():
        return FileResponse(str(file_path))
    return FileResponse(index_html)
"""

import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

# Build a fake dist tree with a secret two levels ABOVE dist.
root = Path(tempfile.mkdtemp())
dist = root / "frontend" / "dist"
dist.mkdir(parents=True)
(dist / "index.html").write_text("<html>INDEX</html>")
# secret.txt sits at root/secret.txt == dist/../../secret.txt
(root / "secret.txt").write_text("TOP-SECRET-CONTENT")

app = FastAPI()
index_html = str(dist / "index.html")


@app.get("/{full_path:path}", include_in_schema=False)
async def _spa_catchall(request: Request, full_path: str):
    file_path = dist / full_path
    if full_path and file_path.is_file():
        return FileResponse(str(file_path))
    return FileResponse(index_html)


client = TestClient(app)

cases = [
    "/normal-page",
    "/../secret.txt",
    "/..%2fsecret.txt",
    "/%2e%2e/secret.txt",
    "/%2e%2e/%2e%2e/secret.txt",
    "/foo/../../secret.txt",
    "/assets/../../secret.txt",
]
for c in cases:
    r = client.get(c)
    body = r.text
    leaked = "TOP-SECRET" in body
    print(f"{c!r:35} -> {r.status_code}  leaked={leaked}  body={body[:40]!r}")
