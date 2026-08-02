#!/usr/bin/env python3
"""Recording man-in-the-middle for the agentalloy proxy's upstream leg.

Sits between agentalloy and the REAL upstream. Every request body agentalloy
forwards is appended to a jsonl log; the response is relayed back byte-for-byte
(including SSE), so qwen-code / codex behave exactly as normal.

    REAL_UPSTREAM=https://api.openai.com python3 record_upstream.py 9999

Then wire the harness at this recorder instead of the real API:

    agentalloy add qwen-code --upstream-url http://localhost:9999/v1

Log: ./upstream-log.jsonl -- one object per request, with the turn number, the
path, and the two fields we care about (`system` / `instructions`).

Driven by ``verify-legs.sh`` in this directory; see the README there.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REAL = os.environ.get("REAL_UPSTREAM", "https://api.openai.com").rstrip("/")
LOG = os.environ.get("UPSTREAM_LOG", "upstream-log.jsonl")
_HOP = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
_turn = 0


def _summarize(body: bytes) -> dict:
    """Pull out just the system leg -- the thing under test."""
    try:
        payload = json.loads(body)
    except Exception:
        return {"parse_error": True, "raw_len": len(body)}

    out: dict = {}
    # Responses API: top-level `instructions`.
    if isinstance(payload.get("instructions"), str):
        out["instructions"] = payload["instructions"]
    # Chat Completions: the system message(s).
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        out["system_messages"] = [
            m.get("content") for m in msgs if isinstance(m, dict) and m.get("role") == "system"
        ]
        last_user = [m for m in msgs if isinstance(m, dict) and m.get("role") == "user"]
        if last_user:
            out["last_user"] = last_user[-1].get("content")
    inp = payload.get("input")
    if isinstance(inp, list):
        out["last_input_item"] = inp[-1] if inp else None
    out["n_tools"] = len(payload.get("tools") or [])
    out["n_messages"] = len(msgs) if isinstance(msgs, list) else None
    out["model"] = payload.get("model")
    out["stream"] = payload.get("stream")
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a: object) -> None:  # silence per-request noise
        pass

    def do_POST(self) -> None:  # noqa: N802
        global _turn
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        _turn += 1

        summary = _summarize(body)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "turn": _turn,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        **summary,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        blocks = 0
        for v in (summary.get("instructions"), *(summary.get("system_messages") or [])):
            if isinstance(v, str):
                blocks += v.count("BEGIN AGENTALLOY-CONTEXT")
        print(
            f"turn {_turn:>2}  {self.path}  AGENTALLOY-CONTEXT blocks on system leg: {blocks}",
            flush=True,
        )

        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
        req = urllib.request.Request(REAL + self.path, data=body, headers=headers, method="POST")
        try:
            upstream = urllib.request.urlopen(req)  # noqa: S310
        except urllib.error.HTTPError as exc:
            upstream = exc
        except Exception as exc:  # upstream unreachable
            msg = json.dumps({"error": {"message": f"recorder: {exc}"}}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(upstream.status)
        for k, v in upstream.headers.items():
            if k.lower() not in _HOP:
                self.send_header(k, v)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        while chunk := upstream.read(1024):
            self.wfile.write(f"{len(chunk):X}\r\n".encode())
            self.wfile.write(chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    print(f"recording -> {REAL}   log: {LOG}   listening on :{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
